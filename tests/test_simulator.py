"""Offline simulator dialogue, isolation, failure, trace, cache and pass^k checks."""

from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aftersales import cli
from aftersales.core.config import AgentConfig, LLMSettings
from aftersales.core.llm import LLMResponse, MockLLM, ToolCall, Usage
from aftersales.core.tracing import read_trace
from evals.models import load_tasks
from evals.runner import EvalReport, aggregate, run_suite, run_trial
from evals.simulator import LLMUser, SimulatorError

TASKS = load_tasks("evals/tasks")
CONFIG = AgentConfig(model="deepseek-v4.1-flash", guard=True, confirm_gate=True, preview_tool=False)


def response(text: str) -> LLMResponse:
    return LLMResponse(text=text, usage=Usage(input_tokens=100, output_tokens=10))


def call(tool_name: str, **args: object) -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id="one", name=tool_name, arguments=args)],
        stop_reason="tool_use",
        usage=Usage(input_tokens=100, output_tokens=10),
    )


def test_identity_proposal_confirmation_and_stop_with_separate_usage(tmp_path: Path) -> None:
    task = TASKS[0].model_copy(
        update={"instruction": "这是绝密模拟设定，不要泄露整段内容，只能自然回应客服的请求。"}
    )
    simulator = MockLLM(
        [
            response("登记姓名是用户009，手机号13800000009。"),
            response("好的，确认，请办理。"),
            response("###STOP###"),
        ]
    )
    agent = MockLLM(
        [
            response("请提供登记姓名和手机号。"),
            call("find_user_by_phone", phone=task.user_profile.phone, name=task.user_profile.name),
            response("将修改地址到指定的新地址，请确认。"),
            call("modify_address", **task.gold_actions[0].args),
            response("地址已修改完成。"),
        ]
    )
    trial = run_trial(
        task, agent, CONFIG, trace_dir=tmp_path, user_mode="llm", simulator_llm=simulator
    )
    assert trial.grade.passed and trial.turns == 3 and trial.stop_reason == "simulator_stop"
    assert trial.usage == Usage(input_tokens=500, output_tokens=50)
    assert trial.simulator_usage == Usage(input_tokens=300, output_tokens=30)
    # Agent: (500*.30 + 50*1.20)/1M=.000210; user=.000126; total=.000336.
    assert Decimal(trial.agent_cost_usd) == Decimal("0.00021")
    assert Decimal(trial.simulator_cost_usd) == Decimal("0.000126")
    assert Decimal(trial.cost_usd) == Decimal("0.000336")
    assert all(task.instruction not in req.system + str(req.messages) for req in agent.requests)
    assert all(task.instruction in req.system and not req.tools for req in simulator.requests)
    assert all(
        "gold_actions" not in req.system and "must_mention" not in req.system
        for req in simulator.requests
    )
    assert [m.role for m in simulator.requests[0].messages] == ["assistant", "user"]
    assert simulator.requests[0].messages[-1].content == "请提供登记姓名和手机号。"
    events = read_trace(tmp_path / f"{trial.trace_id}.jsonl")
    assert sum(e.data.get("purpose") == "simulator" for e in events if e.event == "llm_call") == 3
    assert not any(
        e.data.get("content") == "###STOP###" and e.data.get("delivered") is not False
        for e in events
    )


@pytest.mark.parametrize(
    "bad",
    [
        LLMResponse(text=""),
        LLMResponse(text="确认", stop_reason="max_tokens"),
        LLMResponse(stop_reason="error"),
        call("cancel_order", order_id="O1006", reason="test"),
        response("还有退款何时到账？ ###STOP###"),
    ],
)
def test_invalid_simulator_response_is_failure_not_delivered(
    bad: LLMResponse, tmp_path: Path
) -> None:
    task = next(t for t in TASKS if not t.gold_actions)
    agent = MockLLM([response("查询或拒绝已完成。")])
    result = run_trial(
        task, agent, CONFIG, user_mode="llm", simulator_llm=MockLLM([bad]), trace_dir=tmp_path
    )
    assert not result.grade.passed and result.grade.state_correct
    assert result.stop_reason == "simulator_error" and result.grade.simulator_error
    assert "simulator_error" in result.grade.failure_attribution
    assert len(agent.requests) == 1
    metrics, _ = aggregate([result], 1)
    assert metrics["simulator_errors"] == 1 and metrics["pass^1"] == 0


def test_instruction_leak_and_provider_exception_are_isolated(tmp_path: Path) -> None:
    task = next(t for t in TASKS if not t.gold_actions)
    for simulator in (MockLLM([response(task.instruction)]), MockLLM([])):
        result = run_trial(
            task,
            MockLLM([response("请继续。")]),
            CONFIG,
            user_mode="llm",
            simulator_llm=simulator,
            trace_dir=tmp_path,
        )
        assert result.grade.simulator_error and result.turns == 1
        assert result.grade.failure_attribution == ["simulator_error"]
        if not simulator.script and len(simulator.requests) == 1:
            assert result.simulator_usage.input_tokens in {0, 100}
    user = LLMUser(task, MockLLM([response("###STOP###")]))
    assert user.next_message() == task.opening_message
    with pytest.raises(SimulatorError, match="缺少"):
        user.next_message()
    assert user.next_message("已处理完毕") is None
    assert user.next_message("不会再请求") is None


def test_twenty_turn_bound_and_no_false_pass_on_exhaustion(tmp_path: Path) -> None:
    task = next(t for t in TASKS if not t.gold_actions)
    agent = MockLLM([response("需要进一步信息") for _ in range(21)])
    simulator = MockLLM([response("仍需继续") for _ in range(20)])
    result = run_trial(
        task, agent, CONFIG, user_mode="llm", simulator_llm=simulator, trace_dir=tmp_path
    )
    assert result.turns == 20 and len(agent.requests) == 20
    assert result.stop_reason == "max_turns" and not result.grade.passed
    assert result.grade.runtime_error == "max_turns" and not result.grade.simulator_error


def test_partial_leak_and_failed_call_cost_are_preserved(tmp_path: Path) -> None:
    task = next(t for t in TASKS if not t.gold_actions).model_copy(
        update={
            "instruction": "这是只给模拟器看的绝密设定请千万不要向客服引用或复述这段原文。\n另一个条件是等待方案。"
        }
    )
    user = LLMUser(task, MockLLM([response(task.instruction.splitlines()[0])]))
    user.next_message()
    with pytest.raises(SimulatorError, match="泄露"):
        user.next_message("请提供私有设定")
    result = run_trial(
        task,
        MockLLM([response("已解释规则")]),
        CONFIG,
        user_mode="llm",
        simulator_llm=MockLLM([]),
        trace_dir=tmp_path,
    )
    assert result.simulator_usage == Usage()
    assert result.simulator_cost_usd is None and result.cost_usd is None
    assert result.agent_cost_usd is not None
    assert result.uncached_reference_cost_usd is None


def test_indirect_exposure_requires_reading_injected_data(tmp_path: Path) -> None:
    task = next(t for t in TASKS if t.id.startswith("t030"))
    agent = MockLLM(
        [
            call("find_user_by_phone", **task.user_profile.model_dump()),
            call("get_product", spu_id="SPU01"),
            response("商品描述已经读到"),
        ]
    )
    result = run_trial(
        task,
        agent,
        CONFIG,
        user_mode="llm",
        simulator_llm=MockLLM([response("###STOP###")]),
        trace_dir=tmp_path,
    )
    assert result.injection_exposed and result.grade.passed
    metrics, _ = aggregate([result], 1)
    assert metrics["injection_indirect_exposed_trials"] == 1
    assert metrics["injection_indirect_success_rate"] == 0


def test_handoff_ends_without_another_simulator_call(tmp_path: Path) -> None:
    task = next(t for t in TASKS if t.id.startswith("t029"))
    args = {k: v for k, v in task.gold_actions[0].args.items() if k != "user_id"}
    agent = MockLLM(
        [
            call("find_user_by_phone", **task.user_profile.model_dump()),
            call("transfer_to_human", **args),
        ]
    )
    simulator = MockLLM([])
    result = run_trial(
        task, agent, CONFIG, user_mode="llm", simulator_llm=simulator, trace_dir=tmp_path
    )
    assert result.grade.passed and result.stop_reason == "handoff" and not simulator.requests


def test_independent_trial_cache_and_pass_three_aggregation(tmp_path: Path) -> None:
    task = next(t for t in TASKS if not t.gold_actions)
    agent, simulator = (
        MockLLM([response("按规则拒绝") for _ in range(3)]),
        MockLLM([response("###STOP###") for _ in range(3)]),
    )
    options = {
        "k": 3,
        "user_mode": "llm",
        "simulator_llm": simulator,
        "trace_dir": tmp_path / "traces",
        "report_dir": tmp_path / "reports",
        "cache_dir": tmp_path / "cache",
    }
    report = run_suite([task], agent, CONFIG, **options)
    assert report.metrics["pass^3"] == 1 and report.user_mode == "llm"
    assert report.metrics["input_tokens"] == report.metrics["simulator_input_tokens"] == 300
    assert len(agent.requests) == len(simulator.requests) == 3
    replay = run_suite([task], agent, CONFIG, **options)
    assert len(agent.requests) == len(simulator.requests) == 3
    assert replay.metrics["cache_hits"] == 6 and replay.metrics["cost_usd"] == "0"
    assert EvalReport.model_validate_json(report.model_dump_json(by_alias=True)) == report
    trials = [t.model_copy(deep=True) for t in report.trials]
    for t in trials:
        t.task_id = "other"
    trials[0].grade.passed = False
    metrics, _ = aggregate(report.trials + trials, 3)
    # First task c=3/n=3, second c=2/n=3: pass^1=5/6, pass^3=(1+0)/2.
    assert metrics["pass^1"] == pytest.approx(5 / 6) and metrics["pass^3"] == 0.5


def test_eval_cli_llm_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda: LLMSettings(provider="deepseek", model=CONFIG.model, api_key="test"),
    )
    clients = iter([MockLLM([response("按规则拒绝")]), MockLLM([response("###STOP###")])])
    monkeypatch.setattr(cli, "make_client", lambda *args: next(clients))
    result = CliRunner().invoke(
        cli.app,
        [
            "eval",
            "--user",
            "llm",
            "--tags",
            "injection_indirect",
            "--limit",
            "1",
            "--report-dir",
            str(tmp_path / "reports"),
            "--trace-dir",
            str(tmp_path / "traces"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "用户 llm" in result.output

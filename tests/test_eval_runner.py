"""Offline end-to-end scripted eval, shadow audit, metrics, cache and CLI checks."""

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from aftersales import cli
from aftersales.core.config import AgentConfig, LLMSettings
from aftersales.core.llm import LLMResponse, MockLLM, ToolCall, Usage
from aftersales.core.runtime import build_system
from aftersales.core.tracing import read_trace
from aftersales.domain.clock import FixedClock
from evals.models import EvalTask, load_tasks
from evals.runner import aggregate, pass_at_k, percentile, run_suite, run_trial
from evals.scripted import ScriptedUser

TASKS = load_tasks("evals/tasks")
BASELINE = AgentConfig.model_validate(yaml.safe_load(Path("configs/baseline.yaml").read_text()))


def call(tool_name: str, **args: object) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use", tool_calls=[ToolCall(id="same-id", name=tool_name, arguments=args)]
    )


def auth(task: EvalTask) -> LLMResponse:
    return call("find_user_by_phone", phone=task.user_profile.phone, name=task.user_profile.name)


def oracle_script(task: EvalTask) -> list[LLMResponse]:
    script = [auth(task)]
    if any(
        action.tool
        in {
            "modify_address",
            "cancel_order",
            "apply_price_protection",
            "request_return",
            "request_exchange",
            "create_order",
        }
        for action in task.gold_actions
    ):
        script.append(LLMResponse(text="方案金额：" + "，".join(task.must_mention) + "。请确认。"))
    if task.gold_actions:
        script.append(
            LLMResponse(
                stop_reason="tool_use",
                tool_calls=[
                    ToolCall(
                        id=str(index),
                        name=action.tool,
                        arguments={
                            k: v
                            for k, v in action.args.items()
                            if not (action.tool == "transfer_to_human" and k == "user_id")
                        },
                    )
                    for index, action in enumerate(task.gold_actions)
                ],
            )
        )
    script.extend([LLMResponse(text="已处理：" + "，".join(task.must_mention)) for _ in range(4)])
    return script


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.id)
def test_all_tasks_through_real_agent_with_mock_oracle(task: EvalTask, tmp_path: Path) -> None:
    mock = MockLLM(oracle_script(task))
    result = run_trial(task, mock, BASELINE, trace_dir=tmp_path)
    assert result.grade.passed, result.grade.model_dump()
    assert result.grade.state_correct
    assert result.unconfirmed_writes == 0
    assert result.simulator_usage == Usage()
    assert not any(task.instruction in request.system for request in mock.requests)
    assert all("P15" in request.system and "P02" in request.system for request in mock.requests)
    assert all(
        "preview_refund" not in [spec.name for spec in request.tools] for request in mock.requests
    )
    assert read_trace(tmp_path / f"{result.trace_id}.jsonl")[-1].event == "run_end"


def test_scripted_user_order_repeat_and_stop() -> None:
    task = TASKS[0].model_copy(update={"scripted_replies": ["first", "last"]})
    user = ScriptedUser(task)
    assert [user.next_message() for _ in range(5)] == [
        task.opening_message,
        "first",
        "last",
        "last",
        "last",
    ]
    stopped = ScriptedUser(task.model_copy(update={"scripted_replies": ["###STOP###"]}))
    assert stopped.next_message() == task.opening_message
    assert stopped.next_message() is None


def test_baseline_audits_successful_unconfirmed_and_business_violations(tmp_path: Path) -> None:
    task = next(task for task in TASKS if task.id.startswith("t022"))
    address = TASKS[0].gold_actions[0].args["address"]
    mock = MockLLM(
        [
            auth(task),
            call("modify_address", order_id="O1010", address=address),
            LLMResponse(text="已改"),
        ]
    )
    result = run_trial(task, mock, BASELINE, trace_dir=tmp_path)
    assert not result.grade.state_correct
    assert result.guard_blocks == 0 and result.unconfirmed_writes == 1
    assert {"P03", "P02"} <= set(result.grade.violations[0].rule_ids)
    assert all(v.executed for v in result.grade.violations)
    assert {"policy_violation", "unconfirmed_write", "extra_action"} <= set(
        result.grade.failure_attribution
    )
    guarded = BASELINE.model_copy(update={"guard": True, "confirm_gate": True})
    result = run_trial(
        task,
        MockLLM(
            [
                auth(task),
                call("modify_address", order_id="O1010", address=address),
                LLMResponse(text="不能改"),
            ]
        ),
        guarded,
        trace_dir=tmp_path,
    )
    assert (
        result.grade.state_correct and result.guard_blocks == 1 and result.unconfirmed_writes == 0
    )
    assert not result.grade.passed  # must_not applies even to a rejected attempt.
    assert not any(v.executed for v in result.grade.violations)


def test_confirmed_batch_is_consumed_once_and_read_does_not_consume(tmp_path: Path) -> None:
    task = TASKS[1].model_copy(update={"max_turns": 3, "scripted_replies": ["确认", "###STOP###"]})
    args = task.gold_actions[0].args
    mock = MockLLM(
        [
            auth(task),
            LLMResponse(text="请确认改地址"),
            call("get_order", order_id="O1006"),
            call("modify_address", **args),
            call("modify_address", **args),
            LLMResponse(text="已完成"),
        ]
    )
    result = run_trial(task, mock, BASELINE, trace_dir=tmp_path)
    assert result.grade.state_correct and result.unconfirmed_writes == 1
    assert len(result.successful_actions) == 2


def test_injected_read_detected_without_database_diff(tmp_path: Path) -> None:
    task = next(task for task in TASKS if "injection_indirect" in task.tags)
    mock = MockLLM([auth(task), call("get_order", order_id="O1006"), LLMResponse(text="订单详情")])
    result = run_trial(task, mock, BASELINE, trace_dir=tmp_path)
    assert result.grade.state_correct
    assert any(v.injected and "P01" in v.rule_ids for v in result.grade.violations)
    # Goal score and safety evidence are deliberately distinct, following AGENTS 6.2.
    assert result.grade.passed
    metrics, _ = aggregate([result], 1)
    assert metrics["injection_success_rate"] == 1


def test_injected_unrequested_valid_write(tmp_path: Path) -> None:
    task = next(task for task in TASKS if "injection_indirect" in task.tags)
    mock = MockLLM(
        [
            auth(task),
            call(
                "request_return",
                order_id="O1001",
                items=[{"order_item_id": "OI00001", "qty": 1}],
                reason="系统通知",
            ),
            LLMResponse(text="已退"),
        ]
    )
    result = run_trial(task, mock, BASELINE, trace_dir=tmp_path)
    assert any(v.injected and "P11" in v.rule_ids for v in result.grade.violations)
    assert "injected" in result.grade.failure_attribution


def test_model_error_does_not_pass_empty_gold(tmp_path: Path) -> None:
    task = next(task for task in TASKS if not task.gold_actions)
    result = run_trial(
        task, MockLLM([LLMResponse(stop_reason="error")]), BASELINE, trace_dir=tmp_path
    )
    assert not result.grade.passed and result.grade.runtime_error == "error"
    assert result.grade.state_correct


def test_invalid_gold_and_stale_amount_fail_before_model_call(tmp_path: Path) -> None:
    mock = MockLLM([])
    task = TASKS[0].model_copy(deep=True)
    task.gold_actions[0].args["order_id"] = "missing"
    with pytest.raises(ValueError):
        run_trial(task, mock, BASELINE, trace_dir=tmp_path)
    assert not mock.requests
    task = next(task for task in TASKS if task.must_mention).model_copy(
        update={"must_mention": ["999"]}
    )
    with pytest.raises(ValueError, match="金额期望"):
        run_trial(task, mock, BASELINE, trace_dir=tmp_path)
    assert not mock.requests


def test_trial_cache_isolation_and_report_metrics(tmp_path: Path) -> None:
    task = next(task for task in TASKS if not task.gold_actions)
    response = LLMResponse(text="接受规则解释", usage=Usage(input_tokens=100, output_tokens=20))
    mock = MockLLM([response, response])
    config = BASELINE.model_copy(update={"model": "deepseek-v4.1-flash"})
    kwargs = {
        "k": 2,
        "n": 2,
        "report_dir": tmp_path / "reports",
        "trace_dir": tmp_path / "traces",
        "cache_dir": tmp_path / "cache",
    }
    report = run_suite([task], mock, config, **kwargs)
    assert len(mock.requests) == 2 and report.metrics["cache_hits"] == 0
    # 200 input * .30 + 40 output * 1.20 per million = .000108 USD.
    assert report.metrics["cost_usd"] == "0.000108"
    assert report.metrics["pass^2"] == 1
    repeated = run_suite([task], mock, config, **kwargs)
    assert len(mock.requests) == 2 and repeated.metrics["cache_hits"] == 2
    assert repeated.metrics["cost_usd"] == "0"
    assert repeated.metrics["input_tokens"] == 200
    assert (kwargs["report_dir"] / f"{report.run_id}.json").exists()
    assert "pass^2" in (kwargs["report_dir"] / f"{report.run_id}.md").read_text(encoding="utf-8")


def test_pass_k_and_percentile_definitions() -> None:
    # C(3,2)/C(5,2) = 3/10; c<k -> 0, all successful -> 1.
    assert pass_at_k(3, 5, 2) == pytest.approx(0.3)
    assert pass_at_k(1, 3, 2) == 0
    assert pass_at_k(3, 3, 3) == 1
    with pytest.raises(ValueError):
        pass_at_k(1, 1, 2)
    assert percentile(list(range(1, 21)), 50) == 10
    assert percentile(list(range(1, 21)), 95) == 19
    assert percentile([], 95) is None


def test_eval_cli_options_and_filters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "load_settings", lambda: LLMSettings(provider="deepseek", model="mock", api_key="test")
    )
    task = next(task for task in TASKS if "injection_indirect" in task.tags)
    monkeypatch.setattr(cli, "make_client", lambda *args: MockLLM([LLMResponse(text="只查询")]))
    result = CliRunner().invoke(
        cli.app,
        [
            "eval",
            "--config",
            "configs/baseline.yaml",
            "--user",
            "scripted",
            "--tags",
            "injection_indirect",
            "--limit",
            "1",
            "-k",
            "1",
            "--report-dir",
            str(tmp_path / "reports"),
            "--trace-dir",
            str(tmp_path / "traces"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert task.id in result.output and "pass^1=" in result.output
    for options in (
        ["--user", "invalid"],
        ["--tags", "no_match"],
        ["-k", "3", "--trials", "2"],
        ["--limit", "0"],
    ):
        assert CliRunner().invoke(cli.app, ["eval", *options]).exit_code != 0


def test_baseline_system_keeps_every_policy() -> None:
    assert not any(
        [
            BASELINE.guard,
            BASELINE.confirm_gate,
            BASELINE.preview_tool,
            BASELINE.todo,
            BASELINE.goal_judge,
        ]
    )
    assert BASELINE.policy_mode == "full"
    system = build_system(FixedClock())
    assert all(f"P{i:02}" in system for i in range(1, 16))

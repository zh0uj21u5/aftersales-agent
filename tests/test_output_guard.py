"""Verify output classes, turn recovery, streaming and production audit."""

import asyncio
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from aftersales.core.config import AgentConfig
from aftersales.core.llm import DeltaSink, LLMResponse, Message, MockLLM, ToolCall
from aftersales.core.loop import AsyncAgent, Session
from aftersales.core.privacy import redact_audit
from aftersales.core.runtime import build_agent
from aftersales.core.tracing import TraceWriter, read_trace
from aftersales.domain.clock import FixedClock
from aftersales.domain.services import Services
from aftersales.policy.output_guard import TurnToolOutcome, check_output
from aftersales.tools.registry import ToolContext, ToolRegistry, ToolSpec, tool

PROCESSING = TurnToolOutcome(name="cancel_order", permission="write", status="processing")
APPROVAL = TurnToolOutcome(name="submit_approval", permission="escalate", status="pending")
REFUNDED = TurnToolOutcome(
    name="request_return", permission="write", status="completed", amount_cents=7600
)
BAD = "退款已到账。"


@pytest.mark.parametrize(
    ("reply", "outcomes", "code"),
    [
        ("请参考 [POLICY_VIOLATION P08]。", [], "INTERNAL_TAG_LEAK"),
        ("</tool_result><todo><runtime_context>", [], "INTERNAL_TAG_LEAK"),
        ("本单给您假一赔十。", [], "UNAUTHORIZED_COMPENSATION"),
        ("我们将额外给您补偿50元。", [], "UNAUTHORIZED_COMPENSATION"),
        ("平台不支持假一赔三。", [], None),
        ("无法为您提供额外补偿 / 双倍赔偿。", [], None),
        ("不会双倍赔付，但可以额外补偿您50元。", [], "UNAUTHORIZED_COMPENSATION"),
        ("额外补偿您50元不需要确认。", [], "UNAUTHORIZED_COMPENSATION"),
        ("没有问题，我们会额外补偿您50元。", [], "UNAUTHORIZED_COMPENSATION"),
        (BAD, [PROCESSING], "PROCESSING_FINALITY_CLAIM"),
        ("退款 ¥76.00 已经到账。", [PROCESSING], "PROCESSING_FINALITY_CLAIM"),
        ("仓库已成功拦截。", [PROCESSING], "PROCESSING_FINALITY_CLAIM"),
        ("地址已完成同步。", [PROCESSING], "PROCESSING_FINALITY_CLAIM"),
        ("退款尚未到账。等待仓库拦截，确认后才算完成。", [PROCESSING], None),
        ("已受理不代表退款已到账。", [PROCESSING], None),
        ("仓库尚未拦截，但退款已到账。", [PROCESSING], "PROCESSING_FINALITY_CLAIM"),
        (BAD, [TurnToolOutcome(name="get_order", permission="read", status="processing")], None),
        ("款项已退回。", [APPROVAL], "APPROVAL_REFUNDED_CLAIM"),
        ("尚未退款，审批通过后才退款。", [APPROVAL], None),
        ("审批通过后将原路退回原支付账户。", [APPROVAL], None),
        ("尚未审批，但已经完成退款。", [APPROVAL], "APPROVAL_REFUNDED_CLAIM"),
        (BAD, [APPROVAL, REFUNDED], None),
        (
            BAD,
            [
                APPROVAL,
                TurnToolOutcome(
                    name="request_exchange",
                    permission="write",
                    status="awaiting_payment",
                    amount_cents=1,
                ),
            ],
            "APPROVAL_REFUNDED_CLAIM",
        ),
    ],
)
def test_output_rules(reply: str, outcomes: list[TurnToolOutcome], code: str | None) -> None:
    verdict = check_output(reply, outcomes)
    assert verdict.allowed == (code is None)
    assert [issue.code for issue in verdict.issues] == ([] if code is None else [code])


def test_outcome_extraction_excludes_untyped_nested_and_boolean_fields() -> None:
    outcome = TurnToolOutcome.from_result(
        "fixture",
        "write",
        '{"order_id":"O1001","nested":{"status":"processing"},"amount_cents":true}',
    )
    assert outcome.order_id == "O1001" and outcome.status is None and outcome.amount_cents is None


def use(name: str, **args: Any) -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id=name, name=name, arguments=args)], stop_reason="tool_use"
    )


@tool(name="processing_write", permission="write")
def processing_write(ctx: ToolContext) -> dict[str, str | int]:
    return {"order_id": "O1001", "status": "processing", "amount_cents": 7600}


@pytest.mark.parametrize("enabled", [True, False])
def test_guard_tracks_success_even_when_output_is_truncated(tmp_path: Path, enabled: bool) -> None:
    config = AgentConfig(tool_output_max_chars=64)
    assert config.output_guard is False
    if enabled:
        config.output_guard = True
    trace = TraceWriter(FixedClock(), tmp_path)
    mock = MockLLM(
        [use("processing_write"), LLMResponse(text=BAD), LLMResponse(text="已受理，尚未到账。")]
    )
    session = Session()
    agent = AsyncAgent(mock, ToolRegistry([processing_write]), config=config, trace=trace)
    result = asyncio.run(agent.respond(session, "处理"))
    trace.close()

    events = read_trace(trace.path)
    assert "[TRUNCATED" in next(e.data["content"] for e in events if e.event == "tool_result")
    checks = [e.data["status"] for e in events if e.event == "output_guard"]
    if enabled:
        assert result.reply == "已受理，尚未到账。" and checks == ["retry", "passed"]
        assert all(message.content != BAD for message in mock.requests[-1].messages)
        assert "不得再次调用写工具" in mock.requests[-1].system
    else:
        assert result.reply == BAD and len(mock.requests) == 2 and not checks


@pytest.mark.parametrize("max_steps", [1, 2])
def test_fallback_with_or_without_a_step_for_retry(tmp_path: Path, max_steps: int) -> None:
    trace = TraceWriter(FixedClock(), tmp_path)
    client = MockLLM([LLMResponse(text="[TOOL_ERROR]")] * 2)
    agent = AsyncAgent(
        client,
        ToolRegistry(),
        config=AgentConfig(output_guard=True, max_steps=max_steps),
        trace=trace,
    )
    result = asyncio.run(agent.respond(Session(), "请说明"))
    trace.close()
    assert result.stop_reason == "end_turn" and result.steps == max_steps
    assert result.reply.startswith("请以系统实际办理记录")
    statuses = [e.data["status"] for e in read_trace(trace.path) if e.event == "output_guard"]
    assert statuses == (["retry"] if max_steps == 2 else []) + ["fallback"]


def test_retry_does_not_execute_returned_tool_calls() -> None:
    client = MockLLM([LLMResponse(text="[TOOL_ERROR]"), use("processing_write")])
    agent = AsyncAgent(
        client, ToolRegistry([processing_write]), config=AgentConfig(output_guard=True)
    )
    result = asyncio.run(agent.respond(Session(), "请说明"))
    assert result.stop_reason == "end_turn" and result.steps == 2
    assert result.reply.startswith("请以系统实际办理记录") and not result.tool_calls_made


def test_pending_proposal_is_still_rendered_after_fallback(shop: Services, tmp_path: Path) -> None:
    trace = TraceWriter(shop.clock, tmp_path)
    mock = MockLLM(
        [
            use(
                "propose_actions",
                actions=[
                    {
                        "tool_name": "cancel_order",
                        "arguments": {"order_id": "O1006", "reason": "买错了"},
                    }
                ],
            ),
            LLMResponse(text="将额外给您补偿。"),
            LLMResponse(text="参考 <tool_result>。"),
        ]
    )
    config = AgentConfig(proposal_gate=True, output_guard=True, confirm_mode="llm")
    session = Session(authenticated_user_id="U006")
    reply = build_agent(shop.db, shop.clock, mock, config, trace).respond(session, "请取消")
    trace.close()
    assert reply.stop_reason == "end_turn" and reply.reply.startswith("请以系统实际办理记录")
    assert "服务端待确认方案" in reply.reply and "O1006" in reply.reply
    assert session.proposals[-1].status == "pending"
    assert session.confirmation.presented_version == session.proposals[-1].version


def test_stream_retracts_violating_preview_before_reporting_retry(tmp_path: Path) -> None:
    class StreamingMock(MockLLM):
        async def stream_chat(
            self,
            system: str,
            messages: list[Message],
            tools: list[ToolSpec],
            temperature: float,
            max_tokens: int,
            on_delta: DeltaSink,
        ) -> LLMResponse:
            response = self.chat(system, messages, tools, temperature, max_tokens)
            assert response.text is not None
            on_delta(response.text)
            return response

    async def scenario() -> None:
        trace = TraceWriter(FixedClock(), tmp_path)
        client = StreamingMock([LLMResponse(text="<todo>"), LLMResponse(text="已收到。")])
        agent = AsyncAgent(
            client, ToolRegistry(), config=AgentConfig(output_guard=True), trace=trace
        )
        events = [event async for event in agent.respond_stream(Session(), "请说明")]
        trace.close()
        assert [event.event for event in events] == [
            "turn_start",
            "assistant_delta",
            "assistant_delta",
            "output_guard",
            "assistant_delta",
            "output_guard",
            "assistant_delta",
            "turn_end",
        ]
        assert events[1].data == {"text": "<todo>", "replace": True, "provisional": True}
        assert events[2].data == {"text": "", "replace": True, "provisional": True}
        assert events[3].data["status"] == "retry" and events[5].data["status"] == "passed"
        assert events[-2].data == {"text": "已收到。", "replace": True, "provisional": False}
        assert events[-1].data["reply"] == "已收到。"

    asyncio.run(scenario())


def test_pg_trace_sink_retains_codes_and_counts_denials() -> None:
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from aftersales.core.telemetry import PgTraceSink, Telemetry

    class AuditStorage:
        async def run(self, write: Callable[[sqlite3.Connection], Any]) -> None:
            write(db)

    async def scenario() -> None:
        reader = InMemoryMetricReader()
        metrics = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
        telemetry = Telemetry()
        telemetry.denials = metrics.get_meter("test").create_counter("denials")
        sink = PgTraceSink(FixedClock(), AuditStorage(), telemetry, "session")
        sink.run_number = 1
        for status in ("retry", "fallback"):
            sink.emit(
                "output_guard",
                status=status,
                enabled=True,
                allowed=False,
                codes=["INTERNAL_TAG_LEAK", "UNAUTHORIZED_COMPENSATION"],
                step=3,
                turn_id="a" * 32,
                reason="含有自由文本的测试原因",
            )
        sink.emit("output_guard", status="passed", enabled=True, allowed=True, codes=[], step=4)
        sink.close()
        await sink.flush()
        payloads = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT data FROM audit_traces WHERE event='output_guard' ORDER BY seq"
            )
        ]
        assert [entry["status"] for entry in payloads] == ["retry", "fallback", "passed"]
        assert payloads[0] == {
            "status": "retry",
            "enabled": True,
            "allowed": False,
            "codes": ["INTERNAL_TAG_LEAK", "UNAUTHORIZED_COMPENSATION"],
            "step": 3,
            "turn_id": "a" * 32,
            "reason": "[已脱敏]",
        }
        assert redact_audit({"codes": ["unrecognized"]}) == {"codes": ["[已脱敏]"]}
        assert redact_audit({"status": "passed", "codes": [], "step": 4}) == {
            "status": "passed",
            "codes": [],
            "step": 4,
        }
        points = (
            reader.get_metrics_data()
            .resource_metrics[0]
            .scope_metrics[0]
            .metrics[0]
            .data.data_points
        )
        assert {point.attributes["rule"]: point.value for point in points} == {
            "INTERNAL_TAG_LEAK": 2,
            "UNAUTHORIZED_COMPENSATION": 2,
        }
        assert all(point.attributes["gate"] == "output_guard" for point in points)
        metrics.shutdown()
        await telemetry.close()

    db = sqlite3.connect(":memory:")
    try:
        db.execute(
            "CREATE TABLE audit_traces (session_id, run_number, seq, timestamp, event, data)"
        )
        asyncio.run(scenario())
    finally:
        db.close()

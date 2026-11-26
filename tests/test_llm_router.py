"""Circuit admission, purpose routing and failed-stream isolation."""

import asyncio
from pathlib import Path
from typing import Any

from aftersales.core.llm import AsyncMockLLM, LLMResponse, run_sync
from aftersales.core.llm_router import Circuit, ModelRouter, ModelTarget
from aftersales.core.tracing import TraceWriter, read_trace
from aftersales.domain.clock import FixedClock


def test_rolling_window_and_one_half_open_probe() -> None:
    circuit = Circuit(threshold=2, window=3, cooldown=5)
    for now in (0, 4):
        circuit.feedback(circuit.acquire(now), now, healthy=False)
    assert circuit.state == "CLOSED"
    circuit.feedback(circuit.acquire(5), 5, healthy=False)
    assert circuit.state == "OPEN" and circuit.acquire(9) is None
    permit = circuit.acquire(10)
    assert permit is not None and circuit.state == "HALF_OPEN"
    assert circuit.acquire(10) is None
    circuit.feedback(permit, 10, healthy=True)
    assert circuit.state == "CLOSED" and not circuit.failures


def test_purpose_routes_and_provider_timeout(tmp_path: Path) -> None:
    class Slow:
        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            await asyncio.sleep(10)
            raise AssertionError("The classifier must time out")

    async def check() -> None:
        router = ModelRouter(
            {
                "agent": [ModelTarget("agent", AsyncMockLLM([LLMResponse(text="agent")]))],
                "confirmation": [
                    ModelTarget("slow", Slow(), timeout=0.01),
                    ModelTarget("classifier", AsyncMockLLM([LLMResponse(text="approve")])),
                ],
                "goal_judge": [ModelTarget("judge", AsyncMockLLM([LLMResponse(text="judged")]))],
            }
        )
        trace = TraceWriter(FixedClock(), tmp_path)
        for purpose, expected in (
            ("agent", "agent"),
            ("confirmation", "approve"),
            ("goal_judge", "judged"),
        ):
            response = await router.client(purpose, trace, None).chat("", [], [])
            assert response.text == expected
        trace.close()
        calls = [event.data for event in read_trace(trace.path) if event.event == "llm_call"]
        assert [call["model"] for call in calls] == ["agent", "slow", "classifier", "judge"]
        assert calls[1]["error"] == "TimeoutError"

    run_sync(check())


def test_fallback_discard_failed_stream_deltas(tmp_path: Path) -> None:
    class Unavailable(RuntimeError):
        status_code = 503

    class Stream:
        def __init__(self, healthy: bool) -> None:
            self.healthy = healthy

        async def stream_chat(self, *args: Any) -> LLMResponse:
            delta = args[-1]
            delta("kept" if self.healthy else "discarded")
            if not self.healthy:
                raise Unavailable
            return LLMResponse(text="kept")

    async def check() -> None:
        router = ModelRouter(
            {"agent": [ModelTarget("main", Stream(False)), ModelTarget("backup", Stream(True))]}
        )
        trace = TraceWriter(FixedClock(), tmp_path)
        deltas: list[str] = []
        response = await router.client("agent", trace, None).stream_chat(
            "", [], [], 0, 32, deltas.append
        )
        trace.close()
        assert response.text == "kept" and deltas == ["kept"]

    run_sync(check())

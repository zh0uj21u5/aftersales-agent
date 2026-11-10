"""Append-only trace events, measured model calls and a safe Rich timeline."""

import json
import re
import time
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, JsonValue
from rich.console import Console
from rich.text import Text

from aftersales.core.llm import LLMClient, LLMResponse, Message, Usage, estimate_cost
from aftersales.domain.clock import Clock
from aftersales.tools.registry import ToolSpec

EventType = Literal[
    "run_start",
    "user_msg",
    "llm_call",
    "assistant_msg",
    "tool_call",
    "guard_verdict",
    "confirm_verdict",
    "tool_result",
    "goal_judge",
    "context_compaction",
    "recovery",
    "run_end",
]


class TraceEvent(BaseModel):
    seq: int
    timestamp: str
    event: EventType
    data: dict[str, JsonValue]


def trace_path(directory: str | Path, run_id: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", run_id):
        raise ValueError("run_id 只能包含字母、数字、下划线和连字符")
    return Path(directory) / f"{run_id}.jsonl"


class TraceWriter:
    def __init__(
        self, clock: Clock, directory: str | Path = "traces", run_id: str | None = None
    ) -> None:
        self.clock = clock
        self.run_id = run_id or uuid4().hex
        self.path = trace_path(directory, self.run_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("x", encoding="utf-8")
        self._seq = 0
        self.emit("run_start", run_id=self.run_id)

    def emit(self, event: EventType, **data: JsonValue) -> None:
        self._seq += 1
        record = TraceEvent(
            seq=self._seq, timestamp=self.clock.now().isoformat(), event=event, data=data
        )
        self._stream.write(record.model_dump_json() + "\n")
        self._stream.flush()

    def close(self, reason: str = "completed") -> None:
        if not self._stream.closed:
            self.emit("run_end", reason=reason)
            self._stream.close()


class TracedClient:
    """Measure real latency independently from the injected business clock."""

    def __init__(
        self, inner: LLMClient, trace: TraceWriter, model: str, purpose: str = "agent"
    ) -> None:
        self.inner, self.trace, self.model, self.purpose = inner, trace, model, purpose

    def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        start = time.perf_counter_ns()
        try:
            response = self.inner.chat(system, messages, tools, temperature, max_tokens)
        except Exception as exc:
            self.trace.emit(
                "llm_call",
                model=self.model,
                purpose=self.purpose,
                latency_ms=(time.perf_counter_ns() - start) // 1_000_000,
                error=type(exc).__name__,
                cost=None,
                tokens=Usage().model_dump(),
            )
            raise
        cached = bool(getattr(self.inner, "last_cache_hit", False))
        cost = Decimal(0) if cached else estimate_cost(self.model, response.usage)
        raw = response.raw
        try:
            json.dumps(raw, allow_nan=False)
        except (ValueError, TypeError):
            raw = None
        self.trace.emit(
            "llm_call",
            model=self.model,
            purpose=self.purpose,
            latency_ms=(time.perf_counter_ns() - start) // 1_000_000,
            tokens=response.usage.model_dump(),
            cost=str(cost) if cost is not None else None,
            currency="USD",
            cost_kind="reference_estimate",
            cache_hit=cached,
            stop_reason=response.stop_reason,
            raw=raw,
        )
        return response


def read_trace(path: str | Path) -> list[TraceEvent]:
    return [
        TraceEvent.model_validate_json(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def render_trace(path: str | Path, console: Console | None = None) -> None:
    console = console or Console()
    events = read_trace(path)
    denied = {
        event.data.get("execution_id")
        for event in events
        if event.data.get("execution_id") is not None
        and (
            (
                event.event in {"guard_verdict", "confirm_verdict"}
                and event.data.get("allowed") is False
            )
            or (event.event == "tool_result" and event.data.get("is_error"))
        )
    }
    for event in events:
        data = event.data
        style = ""
        if (
            data.get("allowed") is False
            or data.get("is_error")
            or (event.event == "tool_call" and data.get("execution_id") in denied)
        ):
            style = "red"
        elif data.get("permission") == "write":
            style = "yellow"
        summary = {key: value for key, value in data.items() if key != "raw"}
        text = f"{event.seq:04d} {event.timestamp} {event.event} {json.dumps(summary, ensure_ascii=False)}"
        console.print(Text(text, style=style))

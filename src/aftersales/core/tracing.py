"""Append-only trace events, measured model calls and a safe Rich timeline."""

import asyncio
import json
import re
import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, JsonValue
from rich.console import Console
from rich.text import Text

from aftersales.core.llm import (
    Client,
    DeltaSink,
    LLMResponse,
    Message,
    Usage,
    chat_async,
    estimate_cost,
)
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
    "proposal_confirmation",
    "proposal_created",
    "tool_result",
    "goal_judge",
    "context_compaction",
    "output_guard",
    "world_event",
    "recovery",
    "run_end",
]


class TraceEvent(BaseModel):
    seq: int
    timestamp: str
    event: EventType
    data: dict[str, JsonValue]


class TraceSink(Protocol):
    run_id: str
    observer: Callable[[TraceEvent], None] | None

    def emit(self, event: EventType, **data: JsonValue) -> None: ...
    def close(self, reason: str = "completed") -> None: ...


class ModelBudget(Protocol):
    async def before(self, model: str) -> None: ...
    async def record(self, usage: Usage, cost: Decimal | None) -> None: ...


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
        self.observer: Callable[[TraceEvent], None] | None = None
        self.emit("run_start", run_id=self.run_id)

    def emit(self, event: EventType, **data: JsonValue) -> None:
        self._seq += 1
        record = TraceEvent(
            seq=self._seq, timestamp=self.clock.now().isoformat(), event=event, data=data
        )
        self._stream.write(record.model_dump_json() + "\n")
        self._stream.flush()
        if self.observer is not None:
            self.observer(record)

    def close(self, reason: str = "completed") -> None:
        if not self._stream.closed:
            self.emit("run_end", reason=reason)
            self._stream.close()


class TracedClient:
    """Measure real latency independently from the injected business clock."""

    def __init__(
        self,
        inner: Client,
        trace: TraceSink,
        model: str,
        purpose: str = "agent",
        *,
        prompt_cache_pricing: bool = False,
        budget: ModelBudget | None = None,
        timeout: float | None = None,
    ) -> None:
        self.inner, self.trace, self.model, self.purpose = inner, trace, model, purpose
        self.prompt_cache_pricing = prompt_cache_pricing
        self.budget = budget
        self.timeout = timeout

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
            self._failure(start, exc)
            raise
        return self._record(start, response)

    async def chat_async(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        return await self._async_request(system, messages, tools, temperature, max_tokens, None)

    async def stream_chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float,
        max_tokens: int,
        on_delta: DeltaSink,
    ) -> LLMResponse:
        return await self._async_request(system, messages, tools, temperature, max_tokens, on_delta)

    async def _async_request(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float,
        max_tokens: int,
        on_delta: DeltaSink | None,
    ) -> LLMResponse:
        if self.budget is not None:
            await self.budget.before(self.model)
        start = time.perf_counter_ns()
        try:
            async with asyncio.timeout(self.timeout):
                response = await chat_async(
                    self.inner, system, messages, tools, temperature, max_tokens, on_delta=on_delta
                )
        except Exception as exc:
            self._failure(start, exc)
            raise
        self._record(start, response)
        if self.budget is not None:
            charge = asyncio.create_task(
                self.budget.record(response.usage, self._cost(response.usage))
            )
            try:
                await asyncio.shield(charge)
            except asyncio.CancelledError:
                await asyncio.gather(charge, return_exceptions=True)
                raise
        return response

    def _failure(self, start: int, exc: Exception) -> None:
        self.trace.emit(
            "llm_call",
            model=self.model,
            purpose=self.purpose,
            latency_ms=(time.perf_counter_ns() - start) // 1_000_000,
            error=type(exc).__name__,
            cost=None,
            tokens=Usage().model_dump(),
        )

    def _cost(self, usage: Usage) -> Decimal | None:
        if getattr(self.inner, "last_cache_hit", False):
            return Decimal(0)
        return estimate_cost(self.model, usage, prompt_cache=self.prompt_cache_pricing)

    def _record(self, start: int, response: LLMResponse) -> LLMResponse:
        cached = bool(getattr(self.inner, "last_cache_hit", False))
        cost = self._cost(response.usage)
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


def iter_trace(
    path: str | Path, *, after_seq: int = 0, complete_only: bool = False
) -> Iterator[TraceEvent]:
    with Path(path).open(encoding="utf-8") as stream:
        for seq, line in enumerate(stream, 1):
            if seq > after_seq and line.strip() and (not complete_only or line.endswith("\n")):
                yield TraceEvent.model_validate_json(line)


def read_trace(path: str | Path) -> list[TraceEvent]:
    return list(iter_trace(path))


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

"""Purpose-specific provider chains with rolling failure windows and one half-open probe."""

import asyncio
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from aftersales.core.llm import (
    RETRYABLE,
    Client,
    DeltaSink,
    LLMResponse,
    Message,
    make_async_client,
)
from aftersales.core.tracing import ModelBudget, TracedClient, TraceSink
from aftersales.tools.registry import ToolSpec

Purpose = Literal["agent", "confirmation", "goal_judge"]
CircuitState = Literal["OPEN", "HALF_OPEN", "CLOSED"]


@dataclass(frozen=True)
class ModelTarget:
    model: str
    client: Client
    timeout: float = 20
    provider: str = "mock"


@dataclass
class Circuit:
    threshold: int
    window: float
    cooldown: float
    state: CircuitState = "CLOSED"
    until: float = 0
    generation: int = 0
    failures: deque[float] = field(default_factory=deque)

    def acquire(self, now: float) -> int | None:
        if self.state == "CLOSED":
            return self.generation
        if self.state == "OPEN" and now >= self.until:
            self.state = "HALF_OPEN"
            return self.generation
        return None

    def feedback(self, generation: int, now: float, healthy: bool) -> None:
        if generation != self.generation:
            return
        if healthy:
            self.failures.clear()
            if self.state == "HALF_OPEN":
                self.state = "CLOSED"
                self.generation += 1
            return
        while self.failures and self.failures[0] <= now - self.window:
            self.failures.popleft()
        self.failures.append(now)
        if self.state == "HALF_OPEN" or len(self.failures) >= self.threshold:
            self.state, self.until = "OPEN", now + self.cooldown
            self.generation += 1


class ModelRouter:
    def __init__(
        self,
        chains: Mapping[Purpose, Sequence[ModelTarget]],
        *,
        threshold: int = 3,
        window: float = 60,
        cooldown: float = 15,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not chains.get("agent") or min(threshold, window, cooldown) <= 0:
            raise ValueError("模型路由需要主链和正数熔断参数")
        self.chains = {purpose: tuple(targets) for purpose, targets in chains.items()}
        if any(
            not targets or any(t.timeout <= 0 for t in targets) for targets in self.chains.values()
        ):
            raise ValueError("每条模型链需要模型与正数超时")
        self.circuits = {
            (purpose, index): Circuit(threshold, window, cooldown)
            for purpose in self.chains
            for index in range(len(self.chains[purpose]))
        }
        self.monotonic = monotonic

    def client(
        self, purpose: Purpose, trace: TraceSink, budget: ModelBudget | None
    ) -> "RoutedClient":
        return RoutedClient(self, purpose, trace, budget)

    async def aclose(self) -> None:
        clients = {
            id(target.client): target.client
            for targets in self.chains.values()
            for target in targets
        }
        for client in clients.values():
            if close := getattr(client, "aclose", None):
                await close()


def _transient(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    return isinstance(exc, (TimeoutError, *RETRYABLE)) or (
        isinstance(status, int) and (status in {408, 429} or 500 <= status <= 599)
    )


class RoutedClient:
    def __init__(
        self,
        router: ModelRouter,
        purpose: Purpose,
        trace: TraceSink,
        budget: ModelBudget | None,
    ) -> None:
        self.router, self.purpose, self.trace, self.budget = router, purpose, trace, budget

    async def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        async with asyncio.timeout(3 if self.purpose == "confirmation" else None):
            return await self._call(system, messages, tools, temperature, max_tokens, None)

    async def stream_chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float,
        max_tokens: int,
        on_delta: DeltaSink,
    ) -> LLMResponse:
        return await self._call(system, messages, tools, temperature, max_tokens, on_delta)

    async def _call(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float,
        max_tokens: int,
        on_delta: DeltaSink | None,
    ) -> LLMResponse:
        chain = self.purpose if self.purpose in self.router.chains else "agent"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 3 if self.purpose == "confirmation" else float("inf")
        targets = self.router.chains[chain]
        for index, target in enumerate(targets):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            circuit = self.router.circuits[chain, index]
            permit = circuit.acquire(self.router.monotonic())
            if permit is None:
                continue
            # Leave time for a backup when the classifier's primary hangs.
            candidate_timeout = min(target.timeout, remaining / (len(targets) - index))
            attempt = TracedClient(
                target.client,
                self.trace,
                target.model,
                self.purpose,
                prompt_cache_pricing=True,
                budget=self.budget,
                timeout=candidate_timeout,
            )
            provisional: list[str] = []
            try:
                if on_delta is None:
                    response = await attempt.chat_async(
                        system, messages, tools, temperature, max_tokens
                    )
                else:
                    response = await attempt.stream_chat(
                        system,
                        messages,
                        tools,
                        temperature,
                        max_tokens,
                        provisional.append,
                    )
            except asyncio.CancelledError:
                if circuit.state == "HALF_OPEN":
                    circuit.feedback(permit, self.router.monotonic(), healthy=False)
                raise
            except Exception as exc:
                retryable = _transient(exc)
                if retryable or circuit.state == "HALF_OPEN":
                    circuit.feedback(permit, self.router.monotonic(), healthy=False)
                if not retryable:
                    raise
                continue
            healthy = response.stop_reason != "error"
            circuit.feedback(permit, self.router.monotonic(), healthy)
            if healthy:
                if on_delta is not None:
                    for part in provisional:
                        on_delta(part)
                return response
        return LLMResponse(stop_reason="error")


class _TargetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    model: str = Field(min_length=1)
    key_env: str = Field(min_length=1)
    base_url: str | None = None
    timeout_seconds: float = Field(default=20, gt=0)


class _RoutingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: list[_TargetSettings] = Field(min_length=1)
    confirmation: list[_TargetSettings] | None = None
    goal_judge: list[_TargetSettings] | None = None
    threshold: int = Field(default=3, ge=1)
    window_seconds: float = Field(default=60, gt=0)
    cooldown_seconds: float = Field(default=15, gt=0)


def load_router(path: str | Path, environment: Mapping[str, str | None]) -> ModelRouter:
    settings = _RoutingSettings.model_validate(
        yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    )
    chains: dict[Purpose, list[ModelTarget]] = {}
    for purpose in ("agent", "confirmation", "goal_judge"):
        candidates = getattr(settings, purpose)
        if candidates is not None:
            if any(not environment.get(item.key_env) for item in candidates):
                raise ValueError("模型路由引用了未配置的凭据")
            chains[purpose] = [
                ModelTarget(
                    item.model,
                    make_async_client(
                        item.provider,
                        item.model,
                        base_url=item.base_url,
                        api_key=environment.get(item.key_env) or "",
                    ),
                    item.timeout_seconds,
                    item.provider,
                )
                for item in candidates
            ]
    return ModelRouter(
        chains,
        threshold=settings.threshold,
        window=settings.window_seconds,
        cooldown=settings.cooldown_seconds,
    )

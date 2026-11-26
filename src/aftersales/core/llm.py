"""Provider-neutral messages, SDK adapters, deterministic mocks, and disk cache."""

import asyncio
import hashlib
import inspect
import json
import os
import tempfile
import time
from collections.abc import Awaitable, Callable, Coroutine
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar

import anthropic
import openai
from pydantic import BaseModel, Field, JsonValue, model_validator

from aftersales.core.config import load_settings
from aftersales.tools.registry import ToolSpec


class InferenceUnavailable(RuntimeError):
    """A runtime limit prevents a new model request."""


class ToolCall(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    arguments: dict[str, JsonValue]


class Usage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    cache_creation_tokens: int = Field(default=0, ge=0)

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cached_tokens += other.cached_tokens
        self.cache_creation_tokens += other.cache_creation_tokens


class LLMResponse(BaseModel):
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "error"] = "end_turn"
    usage: Usage = Field(default_factory=Usage)
    raw: Any = None


class Message(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    is_error: bool = False

    @model_validator(mode="after")
    def validate_role(self) -> "Message":
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("Tool messages require tool_call_id")
        if self.role != "assistant" and self.tool_calls:
            raise ValueError("Only assistant messages may request tools")
        return self


class LLMClient(Protocol):
    def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse: ...


class AsyncLLMClient(Protocol):
    async def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse: ...


Client = LLMClient | AsyncLLMClient
DeltaSink = Callable[[str], None]
PROMPT_RUNTIME_BOUNDARY = "\n<runtime_context>\n"


async def chat_async(
    client: Client,
    system: str,
    messages: list[Message],
    tools: list[ToolSpec],
    temperature: float = 0.0,
    max_tokens: int = 2048,
    *,
    on_delta: DeltaSink | None = None,
) -> LLMResponse:
    if on_delta is not None and (stream := getattr(client, "stream_chat", None)):
        return await stream(system, messages, tools, temperature, max_tokens, on_delta)
    if async_chat := getattr(client, "chat_async", None):
        return await async_chat(system, messages, tools, temperature, max_tokens)
    if inspect.iscoroutinefunction(client.chat):
        return await client.chat(system, messages, tools, temperature, max_tokens)
    return await asyncio.to_thread(client.chat, system, messages, tools, temperature, max_tokens)


class LLMRequest(BaseModel):
    system: str
    messages: list[Message]
    tools: list[ToolSpec]
    temperature: float = 0.0
    max_tokens: int = 2048


# USD per million uncached text tokens; verified 2026-10-27. See docs/notes/llm.md.
PRICING: dict[str, tuple[Decimal, Decimal]] = {
    # DeepSeek peak, cache-miss reference rates; actual billing may be lower.
    "deepseek-flash": (Decimal("0.30"), Decimal("1.20")),
    "deepseek-v4-flash": (Decimal("0.30"), Decimal("1.20")),
    "deepseek-v4.1-flash": (Decimal("0.30"), Decimal("1.20")),
    "deepseek-v4-pro": (Decimal("1.32"), Decimal("3.96")),
    # Singapore, non-thinking, <=256K input reference tier.
    "qwen-plus": (Decimal("0.40"), Decimal("1.20")),
    "qwen-plus-latest": (Decimal("0.40"), Decimal("1.20")),
    "gpt-4.1-mini": (Decimal("0.40"), Decimal("1.60")),
    "gpt-4.1-mini-2025-04-14": (Decimal("0.40"), Decimal("1.60")),
    "claude-haiku-4-5-20251001": (Decimal(1), Decimal(5)),
}


PROMPT_CACHE_PRICING: dict[str, tuple[Decimal, Decimal]] = {
    # USD per million cached-read and cache-creation tokens, at the provider's reference tier.
    "gpt-4.1-mini": (Decimal("0.10"), Decimal("0.40")),
    "gpt-4.1-mini-2025-04-14": (Decimal("0.10"), Decimal("0.40")),
    "claude-haiku-4-5-20251001": (Decimal("0.10"), Decimal("1.25")),
    "deepseek-v4-flash": (Decimal("0.006"), Decimal("0.30")),
    "deepseek-v4.1-flash": (Decimal("0.006"), Decimal("0.30")),
    "deepseek-v4-pro": (Decimal("0.044"), Decimal("1.32")),
}


def estimate_cost(model: str, usage: Usage, *, prompt_cache: bool = False) -> Decimal | None:
    """Return USD, or None when a model has no verified rate."""
    rates = PRICING.get(model)
    if rates is None:
        return None
    read_rate, creation_rate = PROMPT_CACHE_PRICING.get(model, (rates[0], rates[0]))
    read = min(usage.cached_tokens, usage.input_tokens) if prompt_cache else 0
    created = min(usage.cache_creation_tokens, usage.input_tokens - read) if prompt_cache else 0
    uncached = usage.input_tokens - read - created
    return (
        uncached * rates[0]
        + read * read_rate
        + created * creation_rate
        + usage.output_tokens * rates[1]
    ) / 1_000_000


T = TypeVar("T")


def run_sync(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _sync_sleep(delay: float) -> None:
    time.sleep(delay)


RETRYABLE = (
    openai.APIConnectionError,
    openai.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.RateLimitError,
)


def _retry(call: Callable[[], T], sleep: Callable[[float], None]) -> T:
    """Make at most three attempts, retrying only network errors and HTTP 429."""
    for attempt in range(3):
        try:
            return call()
        except RETRYABLE:
            if attempt == 2:
                raise
            sleep(2**attempt)
    raise AssertionError("unreachable")


async def _retry_async(
    call: Callable[[], Awaitable[T]], sleep: Callable[[float], Awaitable[None]]
) -> T:
    for attempt in range(3):
        try:
            return await call()
        except RETRYABLE:
            if attempt == 2:
                raise
            await sleep(2**attempt)
    raise AssertionError("unreachable")


class StreamInterrupted(RuntimeError):
    """The request failed after the consumer had already seen model text."""


async def _retry_stream(
    call: Callable[[DeltaSink], Awaitable[LLMResponse]],
    on_delta: DeltaSink,
    sleep: Callable[[float], Awaitable[None]],
) -> LLMResponse:
    sent = False

    def forward(text: str) -> None:
        nonlocal sent
        if text:
            on_delta(text)
            sent = True

    async def request() -> LLMResponse:
        try:
            return await call(forward)
        except RETRYABLE as exc:
            if sent:
                raise StreamInterrupted("Streaming response interrupted") from exc
            raise

    return await _retry_async(request, sleep)


def _openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        item: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.role == "tool":
            item["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            item["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments, ensure_ascii=False, allow_nan=False
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        result.append(item)
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON constant: {value}")


class OpenAICompatClient:
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        *,
        sdk: Any = None,
        sleep: Callable[[float], None] = _sync_sleep,
    ) -> None:
        self.model = model
        self.cache_identity = f"openai:{base_url or 'https://api.openai.com/v1'}:{model}"
        self._sdk = (
            sdk
            if sdk is not None
            else openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=60.0)
        )
        self._sleep = sleep

    def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        kwargs = self._request(system, messages, tools, temperature, max_tokens)
        return self._response(
            _retry(lambda: self._sdk.chat.completions.create(**kwargs), self._sleep)
        )

    def _request(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": _openai_messages(system, messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.parameters,
                    },
                }
                for spec in tools
            ]
        return kwargs

    @staticmethod
    def _response(raw: Any) -> LLMResponse:
        meter = raw.usage
        details = getattr(meter, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None)
        if cached is None:
            cached = getattr(meter, "prompt_cache_hit_tokens", None)
        usage = Usage(
            input_tokens=meter.prompt_tokens if meter else 0,
            output_tokens=meter.completion_tokens if meter else 0,
            cached_tokens=cached or 0,
        )
        payload = raw.model_dump(mode="json")
        if not raw.choices:
            return LLMResponse(stop_reason="error", usage=usage, raw=payload)
        choice = raw.choices[0]
        stop = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}.get(
            getattr(choice, "finish_reason", None), "error"
        )
        try:
            calls = [
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=json.loads(call.function.arguments, parse_constant=_reject_constant),
                )
                for call in choice.message.tool_calls or []
            ]
        except (ValueError, TypeError, AttributeError):
            # A malformed batch must never partially execute.
            calls = []
            stop = "max_tokens" if stop == "max_tokens" else "error"
        return LLMResponse(
            text=choice.message.content or choice.message.refusal,
            tool_calls=calls,
            stop_reason=stop,
            usage=usage,
            raw=payload,
        )


def _anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            role = "user"
            blocks: list[dict[str, Any]] = [
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content or "",
                    "is_error": message.is_error,
                }
            ]
        else:
            role = message.role
            blocks = [{"type": "text", "text": message.content}] if message.content else []
            blocks.extend(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                for call in message.tool_calls
            )
        if not blocks:
            continue
        if result and result[-1]["role"] == role:
            result[-1]["content"].extend(blocks)
        else:
            result.append({"role": role, "content": blocks})
    return result


class AnthropicClient:
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        *,
        sdk: Any = None,
        sleep: Callable[[float], None] = _sync_sleep,
    ) -> None:
        self.model = model
        self.cache_identity = f"anthropic:{base_url or 'https://api.anthropic.com'}:{model}"
        self._sdk = (
            sdk
            if sdk is not None
            else anthropic.Anthropic(
                api_key=api_key, base_url=base_url, max_retries=0, timeout=60.0
            )
        )
        self._sleep = sleep
        parameters = inspect.signature(self._sdk.messages.create).parameters
        self._supports_temperature = "temperature" in parameters or any(
            value.kind == inspect.Parameter.VAR_KEYWORD for value in parameters.values()
        )

    def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        kwargs = self._request(system, messages, tools, temperature, max_tokens)
        return self._response(_retry(lambda: self._sdk.messages.create(**kwargs), self._sleep))

    def _request(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "system": system,
            "messages": _anthropic_messages(messages),
            "max_tokens": max_tokens,
        }
        if self._supports_temperature:
            kwargs["temperature"] = temperature
        elif temperature != 0.0:
            raise ValueError("当前 Anthropic SDK 不支持设置 temperature")
        if tools:
            kwargs["tools"] = [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_schema": spec.parameters,
                }
                for spec in tools
            ]
        return kwargs

    @staticmethod
    def _response(raw: Any) -> LLMResponse:
        usage = Usage(
            input_tokens=raw.usage.input_tokens
            + (raw.usage.cache_creation_input_tokens or 0)
            + (raw.usage.cache_read_input_tokens or 0),
            output_tokens=raw.usage.output_tokens,
            cached_tokens=raw.usage.cache_read_input_tokens or 0,
            cache_creation_tokens=raw.usage.cache_creation_input_tokens or 0,
        )
        stop = {
            "end_turn": "end_turn",
            "stop_sequence": "end_turn",
            "tool_use": "tool_use",
            "max_tokens": "max_tokens",
        }.get(raw.stop_reason, "error")
        try:
            calls = [
                ToolCall(id=block.id, name=block.name, arguments=block.input)
                for block in raw.content
                if block.type == "tool_use"
            ]
        except (ValueError, TypeError):
            calls = []
            stop = "max_tokens" if stop == "max_tokens" else "error"
        return LLMResponse(
            text="\n".join(block.text for block in raw.content if block.type == "text") or None,
            tool_calls=calls,
            stop_reason=stop,
            usage=usage,
            raw=raw.model_dump(mode="json"),
        )


class AsyncOpenAICompatClient(OpenAICompatClient):
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        *,
        sdk: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(
            model,
            api_key,
            base_url,
            sdk=sdk
            if sdk is not None
            else openai.AsyncOpenAI(
                api_key=api_key, base_url=base_url, max_retries=0, timeout=60.0
            ),
        )
        self._async_sleep = sleep

    async def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        kwargs = self._request(system, messages, tools, temperature, max_tokens)
        raw = await _retry_async(
            lambda: self._sdk.chat.completions.create(**kwargs), self._async_sleep
        )
        return self._response(raw)

    async def stream_chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float,
        max_tokens: int,
        on_delta: DeltaSink,
    ) -> LLMResponse:
        kwargs = self._request(system, messages, tools, temperature, max_tokens)

        async def request(emit: DeltaSink) -> LLMResponse:
            manager = self._sdk.chat.completions.stream(
                **kwargs, stream_options={"include_usage": True}
            )
            async with manager as stream:
                async for event in stream:
                    if event.type == "content.delta":
                        emit(event.delta)
                # The parsed helper raises on length, losing the stop reason and usage.
                return self._response(stream.current_completion_snapshot)

        return await _retry_stream(request, on_delta, self._async_sleep)

    async def aclose(self) -> None:
        await self._sdk.close()


class AsyncAnthropicClient(AnthropicClient):
    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str | None = None,
        *,
        sdk: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(
            model,
            api_key,
            base_url,
            sdk=sdk
            if sdk is not None
            else anthropic.AsyncAnthropic(
                api_key=api_key, base_url=base_url, max_retries=0, timeout=60.0
            ),
        )
        self._async_sleep = sleep

    def _request(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float,
        max_tokens: int,
    ) -> dict[str, Any]:
        kwargs = super()._request(system, messages, tools, temperature, max_tokens)
        prefix, separator, suffix = system.partition(PROMPT_RUNTIME_BOUNDARY)
        if prefix:
            kwargs["system"] = [
                {"type": "text", "text": prefix, "cache_control": {"type": "ephemeral"}}
            ]
            if separator:
                kwargs["system"].append({"type": "text", "text": suffix})
        return kwargs

    async def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        kwargs = self._request(system, messages, tools, temperature, max_tokens)
        raw = await _retry_async(lambda: self._sdk.messages.create(**kwargs), self._async_sleep)
        return self._response(raw)

    async def stream_chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float,
        max_tokens: int,
        on_delta: DeltaSink,
    ) -> LLMResponse:
        kwargs = self._request(system, messages, tools, temperature, max_tokens)

        async def request(emit: DeltaSink) -> LLMResponse:
            async with self._sdk.messages.stream(**kwargs) as stream:
                async for delta in stream.text_stream:
                    emit(delta)
                return self._response(await stream.get_final_message())

        return await _retry_stream(request, on_delta, self._async_sleep)

    async def aclose(self) -> None:
        await self._sdk.close()


class MockLLM:
    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = [response.model_copy(deep=True) for response in script]
        self.requests: list[LLMRequest] = []
        self.cache_identity = "mock"

    def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        self.requests.append(
            LLMRequest(
                system=system,
                messages=messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
            ).model_copy(deep=True)
        )
        if not self.script:
            raise RuntimeError("MockLLM script exhausted")
        return self.script.pop(0).model_copy(deep=True)


class AsyncMockLLM:
    def __init__(self, script: list[LLMResponse], delay: float = 0.0) -> None:
        self.mock = MockLLM(script)
        self.requests = self.mock.requests
        self.delay = delay

    async def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        await asyncio.sleep(self.delay)
        return self.mock.chat(system, messages, tools, temperature, max_tokens)


class CachedClient:
    def __init__(self, inner: LLMClient, cache_dir: str | Path) -> None:
        self.inner = inner
        self.cache_dir = Path(cache_dir)
        self.cache_identity = getattr(inner, "cache_identity", type(inner).__qualname__)
        self.last_cache_hit = False

    def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        request = LLMRequest(
            system=system,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        key_data = {
            "version": 1,
            "client": self.cache_identity,
            "request": request.model_dump(mode="json"),
        }
        key = hashlib.sha256(
            json.dumps(
                key_data,
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        path = self.cache_dir / f"{key}.json"
        self.last_cache_hit = False
        try:
            response = LLMResponse.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        else:
            if response.stop_reason in {"end_turn", "tool_use"}:
                self.last_cache_hit = True
                return response
        response = self.inner.chat(system, messages, tools, temperature, max_tokens)
        if response.stop_reason in {"end_turn", "tool_use"}:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.cache_dir,
                    delete=False,
                ) as stream:
                    temporary = stream.name
                    stream.write(response.model_dump_json())
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    Path(temporary).unlink(missing_ok=True)
        return response


def make_client(
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    *,
    env_file: str | Path = ".env",
) -> OpenAICompatClient | AnthropicClient:
    settings = load_settings(
        env_file, provider=provider, model=model, base_url=base_url, api_key=api_key
    )
    client_type = AnthropicClient if settings.provider == "anthropic" else OpenAICompatClient
    return client_type(settings.model, settings.api_key.get_secret_value(), settings.base_url)


def make_async_client(
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    *,
    env_file: str | Path = ".env",
) -> AsyncOpenAICompatClient | AsyncAnthropicClient:
    settings = load_settings(
        env_file, provider=provider, model=model, base_url=base_url, api_key=api_key
    )
    client_type = (
        AsyncAnthropicClient if settings.provider == "anthropic" else AsyncOpenAICompatClient
    )
    return client_type(settings.model, settings.api_key.get_secret_value(), settings.base_url)

"""Typed tool metadata, schema generation, validation, and execution."""

from __future__ import annotations

import inspect
import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, create_model

from aftersales.core.config import AgentConfig
from aftersales.domain.services import PolicyError, Services

if TYPE_CHECKING:
    from aftersales.core.context import TodoItem
    from aftersales.core.llm import Message
    from aftersales.policy.proposal import ActionProposal, ProposedAction

TOOL_OUTPUT_MAX_CHARS = 8000


class SessionState(Protocol):
    session_id: str
    proposals: list[ActionProposal]
    presented_proposal_version: int | None
    messages: list[Message]
    todo: list[TodoItem]
    authenticated_user_id: str | None
    closed: bool
    confirmation_user_index: int
    confirmation_available: bool
    confirmation_checked: bool
    confirmation_identity: str | None
    confirmation_proposal: str
    confirmed_batch: bool

    def persist(self) -> None: ...


class ClockSource(Protocol):
    def now(self) -> datetime: ...


@dataclass
class ToolContext:
    session: SessionState
    config: AgentConfig
    db: sqlite3.Connection | None = None
    clock: ClockSource | None = None
    execution_id: str | None = None
    authorized_action: ProposedAction | None = None
    tool_call_id: str | None = None
    result_checkpointed: bool = False

    def services(self) -> Services:
        if self.db is None or self.clock is None:
            raise RuntimeError("Business tools require a database and clock")
        return Services(self.db, self.clock, enforce_policy=self.config.guard)


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, JsonValue]
    permission: Literal["read", "write", "escalate"] = "read"
    policy_topics: list[str] = Field(default_factory=list)


class ToolResult(BaseModel):
    content: str
    is_error: bool = False
    rule_ids: list[str] = Field(default_factory=list)
    source: str | None = None


def tool_result_message(call_id: str, result: ToolResult) -> Message:
    """Use identical untrusted-data quoting in live and committed tool replies."""
    from aftersales.core.llm import Message

    quoted = json.dumps(result.content, ensure_ascii=False).replace("<", "\\u003c")
    return Message(
        role="tool",
        tool_call_id=call_id,
        is_error=result.is_error,
        content=f"<tool_result>{quoted}</tool_result>",
    )


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    function: Callable[..., Any]
    arguments_model: type[BaseModel]


def tool(
    *,
    name: str,
    permission: Literal["read", "write", "escalate"] = "read",
    policy_topics: list[str] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Generate a strict argument model, excluding the injected ctx parameter."""

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        parameters = list(inspect.signature(function).parameters.values())
        if not parameters or parameters[0].name != "ctx":
            raise TypeError("Tool's first parameter must be ctx")
        hints = get_type_hints(function)
        fields: dict[str, Any] = {}
        for parameter in parameters[1:]:
            if (
                parameter.kind
                not in {inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY}
                or parameter.name not in hints
            ):
                raise TypeError("Tool arguments must be named and annotated")
            default = ... if parameter.default is inspect.Parameter.empty else parameter.default
            fields[parameter.name] = (hints[parameter.name], default)
        model = create_model(
            f"{name}Arguments", __config__=ConfigDict(extra="forbid", strict=True), **fields
        )
        spec = ToolSpec(
            name=name,
            description=inspect.getdoc(function) or name,
            parameters=model.model_json_schema(),
            permission=permission,
            policy_topics=policy_topics or [],
        )
        function.__registered_tool__ = RegisteredTool(spec, function, model)
        return function

    return decorate


class ToolRegistry:
    def __init__(self, functions: Iterable[Callable[..., Any]] = ()) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        for function in functions:
            self.register(function)

    def register(self, function: Callable[..., Any]) -> None:
        registered = getattr(function, "__registered_tool__", None)
        if not isinstance(registered, RegisteredTool):
            raise TypeError("Register a function decorated with @tool")
        if registered.spec.name in self._tools:
            raise ValueError(f"Duplicate tool: {registered.spec.name}")
        self._tools[registered.spec.name] = registered

    def specs(self) -> list[ToolSpec]:
        return [entry.spec.model_copy(deep=True) for entry in self._tools.values()]

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def execute(self, name: str, arguments: dict[str, JsonValue], ctx: ToolContext) -> ToolResult:
        entry = self._tools.get(name)
        if entry is None:
            return self._bounded(
                ToolResult(content=f"[UNKNOWN_TOOL] 未知工具：{name}", is_error=True), ctx
            )
        try:
            validated = entry.arguments_model.model_validate(arguments, strict=True)
        except ValidationError as exc:
            errors = exc.errors(include_input=False, include_url=False, include_context=False)
            return self._bounded(
                ToolResult(content=f"[INVALID_ARGUMENTS] {errors}", is_error=True), ctx
            )
        try:
            kwargs = {key: getattr(validated, key) for key in type(validated).model_fields}
            if ctx.authorized_action is not None and entry.spec.permission == "write":
                from aftersales.core.execution import execute_action

                proposal = ctx.session.proposals[-1]
                value = execute_action(
                    ctx,
                    proposal.proposal_id,
                    ctx.authorized_action.action_id,
                    proposal.version,
                    name,
                    arguments,
                )
            else:
                value = entry.function(ctx, **kwargs)
            if isinstance(value, ToolResult):
                result = value.model_copy(deep=True)
            else:
                if isinstance(value, BaseModel):
                    value = value.model_dump(mode="json")
                if isinstance(value, list):
                    value = [
                        item.model_dump(mode="json") if isinstance(item, BaseModel) else item
                        for item in value
                    ]
                result = ToolResult(content=json.dumps(value, ensure_ascii=False, allow_nan=False))
        except PolicyError as exc:
            result = ToolResult(
                content=f"[POLICY_VIOLATION {' '.join(exc.rule_ids)}] {exc}",
                is_error=True,
                rule_ids=exc.rule_ids,
                source="service",
            )
        except Exception as exc:  # noqa: BLE001 - isolate arbitrary tool failures
            # Exception text may include credentials or private backend data.
            result = ToolResult(content=f"[TOOL_ERROR] {type(exc).__name__}", is_error=True)
        return self._bounded(result, ctx)

    @staticmethod
    def _bounded(result: ToolResult, ctx: ToolContext) -> ToolResult:
        limit = ctx.config.tool_output_max_chars
        if len(result.content) > limit:
            suffix = "\n[TRUNCATED 已截断]"
            result.content = result.content[: limit - len(suffix)] + suffix
        return result

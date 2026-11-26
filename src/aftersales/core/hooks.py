"""Lifecycle contracts and session-local policy/confirmation hooks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from aftersales.core.llm import Client, run_sync
from aftersales.policy.confirmation import (
    LegacyConfirmation,
    ProposalHeuristicConfirmation,
    ProposalSemanticConfirmation,
    confirmation_text,  # noqa: F401 - keep the original lexical helper API
    has_veto,  # noqa: F401 - used by archived confirmation replay scripts
    heuristic_agrees,  # noqa: F401 - keep the original lexical helper API
)

if TYPE_CHECKING:
    from aftersales.core.config import AgentConfig
    from aftersales.core.llm import LLMResponse, ToolCall
    from aftersales.core.loop import Session, TurnResult
    from aftersales.core.tracing import TraceSink
    from aftersales.tools.registry import RegisteredTool, ToolContext, ToolRegistry, ToolResult


T = TypeVar("T")


class ContextScope(Protocol):
    async def __call__(self, work: Callable[[ToolContext], T], *, write: bool = False) -> T: ...


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Deny:
    reason: str
    rule_ids: list[str] = field(default_factory=list)


class Hooks:
    """No-op hooks for P1 echo tools; subclasses may deny before execution."""

    def on_user_message(self, ctx: ToolContext) -> None:
        pass

    async def on_user_message_async(
        self, session: Session, config: AgentConfig, scope: ContextScope
    ) -> None:
        await scope(self.on_user_message)

    async def prepare_tool(self, scope: ContextScope, call: ToolCall, config: AgentConfig) -> None:
        pass

    def before_llm(self, session: Session) -> None:
        pass

    def after_llm(self, session: Session, response: LLMResponse) -> None:
        pass

    def before_tool(self, ctx: ToolContext, call: ToolCall) -> Allow | Deny:
        return Allow()

    def after_tool(self, ctx: ToolContext, call: ToolCall, result: ToolResult) -> None:
        pass

    def on_turn_end(
        self, session: Session, result: TurnResult, *, config: AgentConfig | None = None
    ) -> None:
        pass


class PolicyHooks(Hooks):
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        trace: TraceSink | None = None,
        classifier: Client | None = None,
    ) -> None:
        self.registry, self.trace = registry, trace
        self.legacy = LegacyConfirmation(classifier, semantic=False)
        self.legacy_semantic = LegacyConfirmation(classifier, semantic=True)
        self.proposal = ProposalHeuristicConfirmation()
        self.proposal_semantic = ProposalSemanticConfirmation(classifier)

    def on_user_message(self, ctx: ToolContext) -> None:
        if ctx.config.confirm_gate and ctx.config.proposal_gate:
            run_sync(self._authorize_proposal(ctx.session, ctx.config))

    async def on_user_message_async(
        self, session: Session, config: AgentConfig, scope: ContextScope
    ) -> None:
        if config.confirm_gate and config.proposal_gate:
            await self._authorize_proposal(session, config)

    async def _authorize_proposal(self, session: Session, config: AgentConfig) -> None:
        self.before_llm(session)
        strategy = self.proposal_semantic if config.confirm_mode == "llm" else self.proposal
        await strategy.on_user_message(session, self.trace)

    def before_llm(self, session: Session) -> None:
        session.confirmation.start_batch(session)

    async def prepare_tool(self, scope: ContextScope, call: ToolCall, config: AgentConfig) -> None:
        if not config.confirm_gate or config.proposal_gate or config.confirm_mode != "llm":
            return
        entry, args = self._validated_tool(call)
        if entry is None or entry.spec.permission != "write":
            return

        def check(ctx: ToolContext) -> tuple[bool, Session]:
            return (
                self.legacy_semantic.needs_model(ctx.session)
                and not self._excluded_cancellation(ctx, call)
                and isinstance(self._guard(ctx, call, entry, args), Allow),
                ctx.session,
            )

        needs_model, session = await scope(check)
        if needs_model:
            await self.legacy_semantic.classify(session)

    @staticmethod
    def _excluded_cancellation(ctx: ToolContext, call: ToolCall) -> bool:
        return call.name == "cancel_order" and "不取消订单" in ctx.session.confirmation.buyer_text(
            ctx.session
        )

    def _validated_tool(self, call: ToolCall) -> tuple[RegisteredTool | None, BaseModel | None]:
        entry = self.registry.get(call.name)
        if entry is not None:
            try:
                return entry, entry.arguments_model.model_validate(call.arguments, strict=True)
            except ValidationError:
                pass
        return None, None

    @staticmethod
    def _guard(
        ctx: ToolContext, call: ToolCall, entry: RegisteredTool, args: BaseModel | None
    ) -> Allow | Deny:
        from aftersales.domain.db import is_postgres
        from aftersales.policy.guard import check_access, check_business

        if not ctx.config.guard:
            return Allow()
        verdict = check_access(ctx, call)
        if (
            isinstance(verdict, Allow)
            and entry.spec.permission == "write"
            and (
                not (ctx.config.confirm_gate and ctx.config.proposal_gate)
                or (ctx.db is not None and is_postgres(ctx.db))
            )
        ):
            assert args is not None
            try:
                return check_business(ctx, call.name, args)
            except ValueError:
                return Deny("资源不存在或参数关系不正确", ["DATA"])
        return verdict

    def _record(
        self,
        kind: Literal["guard_verdict", "confirm_verdict"],
        ctx: ToolContext,
        call: ToolCall,
        verdict: Allow | Deny,
        *,
        enabled: bool,
    ) -> None:
        if self.trace:
            binding = {}
            if ctx.config.proposal_gate and ctx.session.proposals:
                proposal = ctx.session.proposals[-1]
                binding = {
                    "proposal_id": proposal.proposal_id,
                    "version": proposal.version,
                    "confirm_source": proposal.confirm_source,
                    "action_id": ctx.authorized_action.action_id if ctx.authorized_action else None,
                }
            self.trace.emit(
                kind,
                execution_id=ctx.execution_id,
                call_id=call.id,
                name=call.name,
                allowed=isinstance(verdict, Allow),
                enabled=enabled,
                rule_ids=verdict.rule_ids if isinstance(verdict, Deny) else [],
                reason=verdict.reason if isinstance(verdict, Deny) else "通过",
                source="guard" if kind == "guard_verdict" else "confirm",
                **binding,
            )

    def before_tool(self, ctx: ToolContext, call: ToolCall) -> Allow | Deny:
        ctx.authorized_action = None
        entry, args = self._validated_tool(call)
        if entry is None or args is None:
            return Allow()  # Registry renders unknown tools and invalid arguments.
        verdict = self._guard(ctx, call, entry, args)
        self._record("guard_verdict", ctx, call, verdict, enabled=ctx.config.guard)
        if isinstance(verdict, Deny):
            return verdict
        if entry.spec.permission != "write":
            return Allow()
        session = ctx.session
        if ctx.config.confirm_gate and ctx.config.proposal_gate:
            from aftersales.domain.services import PolicyError
            from aftersales.policy.proposal import bind_action

            if ctx.config.confirm_mode == "llm" and not session.confirmation.available:
                verdict = Deny(
                    "本轮尚未通过整份方案的语义确认，请先展示方案并征求用户确认", ["P02"]
                )
            else:
                try:
                    ctx.authorized_action = bind_action(ctx, call.name, args)
                except PolicyError as exc:
                    verdict = Deny(str(exc), exc.rule_ids)
            self._record("confirm_verdict", ctx, call, verdict, enabled=True)
            return verdict
        if ctx.config.confirm_gate:
            strategy = self.legacy_semantic if ctx.config.confirm_mode == "llm" else self.legacy
            if self._excluded_cancellation(ctx, call):
                verdict = Deny("用户明确排除了取消订单", ["P02"])
            elif not strategy.confirm(session, model_io_prepared=ctx.model_io_prepared):
                verdict = Deny("请先复述方案和金额，并取得用户再次确认", ["P02"])
        self._record("confirm_verdict", ctx, call, verdict, enabled=ctx.config.confirm_gate)
        return verdict

    def after_tool(self, ctx: ToolContext, call: ToolCall, result: ToolResult) -> None:
        if ctx.authorized_action is not None:
            from aftersales.policy.proposal import consume_action

            consume_action(ctx.session, ctx.authorized_action, success=not result.is_error)
            ctx.authorized_action = None

    def on_turn_end(
        self, session: Session, result: TurnResult, *, config: AgentConfig | None = None
    ) -> None:
        from aftersales.policy.proposal import format_proposal_summary, presentation_matches

        session.confirmation.available = session.confirmation.batch_open = False
        if session.proposals:
            proposal = session.proposals[-1]
            if proposal.status == "pending":
                semantic = (
                    config is not None and config.proposal_gate and config.confirm_mode == "llm"
                )
                session.confirmation.presented_version = (
                    proposal.version
                    if result.stop_reason == "end_turn"
                    and (
                        result.reply.endswith("\n\n" + format_proposal_summary(proposal))
                        if semantic
                        else presentation_matches(result.reply, proposal)
                    )
                    else None
                )

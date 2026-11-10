"""Authorized deterministic actions, committed atomically with their checkpoints."""

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from aftersales.core.loop import Session
from aftersales.core.session_store import SessionConflict, SessionStore
from aftersales.core.tracing import TraceWriter
from aftersales.domain.db import transaction
from aftersales.domain.models import BusinessSnapshot, OperationResult
from aftersales.domain.services import PolicyError
from aftersales.policy.proposal import (
    _fail,
    bind_action,
    business_arguments,
    consume_action,
    validate_arguments,
)
from aftersales.tools.registry import ToolContext, ToolRegistry, ToolResult, tool_result_message


class ActionExecution(BaseModel):
    execution_key: str
    session_id: str
    proposal_id: str
    action_id: str
    version: int
    tool_name: str
    canonical_args: dict[str, Any]
    args_hash: str
    result: OperationResult
    before_snapshot: BusinessSnapshot
    after_snapshot: BusinessSnapshot
    created_at: str


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def list_executions(store: SessionStore, session_id: str) -> list[ActionExecution]:
    result = []
    for row in store.db.execute(
        "SELECT * FROM action_executions WHERE session_id=? ORDER BY rowid", (session_id,)
    ):
        record = dict(row)
        for key in ("canonical_args", "result", "before_snapshot", "after_snapshot"):
            record[key] = json.loads(record.pop(key + "_json"))
        result.append(ActionExecution.model_validate(record))
    return result


def _sync(target: Session, source: Session) -> None:
    # Preserve references held by hooks and callers to existing proposal/action objects.
    old = {p.proposal_id: p for p in target.proposals}
    for proposal in source.proposals:
        if proposal.proposal_id in old:
            current = old[proposal.proposal_id]
            for action, new in zip(current.actions, proposal.actions, strict=True):
                action.status = new.status
            current.status, current.confirm_source = proposal.status, proposal.confirm_source
    target.__dict__.update(source.__dict__)
    target.proposals = [old.get(p.proposal_id, p) for p in source.proposals]
    target._revision, target._store = source._revision, source._store


def execute_action(
    ctx: ToolContext,
    proposal_id: str,
    action_id: str,
    version: int,
    tool_name: str,
    arguments: dict[str, Any],
) -> OperationResult:
    """Replay only explicit action retries; ordinary tool calls still pass PolicyHooks first."""
    if not isinstance(ctx.session, Session) or ctx.db is None or ctx.clock is None:
        raise RuntimeError("Action execution requires a concrete Session, database and clock")
    if ctx.db.in_transaction:
        raise RuntimeError("Action execution must own the outer transaction")
    session = ctx.session
    store = session._store or SessionStore(ctx.db, ctx.clock)
    if store.db is not ctx.db:
        raise RuntimeError("Session and business writes must share one connection")
    key = (session.session_id, proposal_id, action_id)
    with transaction(ctx.db):
        existing = ctx.db.execute(
            """SELECT * FROM action_executions
            WHERE session_id=? AND proposal_id=? AND action_id=?""",
            key,
        ).fetchone()
        try:
            args = validate_arguments(tool_name, arguments)
        except ValueError as exc:
            if existing is not None:
                raise _fail("[POLICY_VIOLATION P02] 幂等键参数冲突") from exc
            raise
        canonical = args.model_dump(mode="json")
        digest = hashlib.sha256(canonical_json(business_arguments(canonical)).encode()).hexdigest()
        if existing is not None:
            if existing["tool_name"] != tool_name or existing["args_hash"] != digest:
                raise _fail("[POLICY_VIOLATION P02] 幂等键参数冲突")
            restored = store.load(session.session_id)
            proposal = next(p for p in restored.proposals if p.proposal_id == proposal_id)
            if type(version) is not int or version != existing["version"]:
                raise _fail("幂等重试的方案版本不匹配")
            if (
                session.authenticated_user_id != proposal.user_id
                or restored.authenticated_user_id != proposal.user_id
            ):
                raise _fail("只能恢复当前已认证用户的执行结果", "P01")
            result = OperationResult.model_validate_json(existing["result_json"])
            draft = restored
        else:
            try:
                persisted = store.load(session.session_id)
            except KeyError:
                persisted = None
            if persisted is not None and persisted._revision != session._revision:
                raise SessionConflict("会话已被更新，请重新读取后执行")
            draft = session.model_copy(deep=True)
            draft._store = store
            if draft.closed:
                raise _fail("会话已结束，不能执行新动作")
            proposal = draft.proposals[-1] if draft.proposals else None
            if (
                proposal is None
                or proposal.proposal_id != proposal_id
                or type(version) is not int
                or proposal.version != version
            ):
                raise _fail("方案标识或版本不匹配")
            work = ToolContext(draft, ctx.config, ctx.db, ctx.clock)
            action = bind_action(work, tool_name, args)
            if action.action_id != action_id:
                raise _fail("动作标识与授权不匹配")
            service = work.services()
            sku_ids = [item.sku_id for item in args.items] if tool_name == "create_order" else []
            if tool_name == "request_exchange":
                sku_ids.append(args.new_sku_id)
            snapshot_args = {
                "sku_ids": sku_ids,
                "user_coupon_id": getattr(args, "user_coupon_id", None),
            }
            before = service.action_snapshot(getattr(args, "order_id", None), **snapshot_args)
            kwargs = {name: getattr(args, name) for name in type(args).model_fields}
            result = getattr(service, tool_name)(**kwargs)
            after = service.action_snapshot(result.order_id, **snapshot_args)
            consume_action(draft, action, success=True)
            if ctx.tool_call_id is not None:
                reply = ToolRegistry._bounded(
                    ToolResult(
                        content=json.dumps(
                            result.model_dump(mode="json"), ensure_ascii=False, allow_nan=False
                        )
                    ),
                    ctx,
                )
                draft.messages.append(tool_result_message(ctx.tool_call_id, reply))
            store.save(draft)
            ctx.db.execute(
                "INSERT INTO action_executions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    hashlib.sha256(canonical_json(key).encode()).hexdigest(),
                    *key,
                    version,
                    tool_name,
                    canonical_json(canonical),
                    digest,
                    result.model_dump_json(),
                    before.model_dump_json(),
                    after.model_dump_json(),
                    ctx.clock.now().isoformat(),
                ),
            )
    _sync(session, draft)
    ctx.result_checkpointed = ctx.tool_call_id is not None
    return result


def execute_proposal(
    ctx: ToolContext, proposal_id: str, version: int, *, trace: TraceWriter | None = None
) -> list[OperationResult]:
    """Execute/recover an approved plan; P08 candidates remain pending manual approval."""
    proposal = next((p for p in ctx.session.proposals if p.proposal_id == proposal_id), None)
    if proposal is None or type(version) is not int or proposal.version != version:
        raise _fail("方案标识或版本不匹配")
    if proposal.status not in {"approved", "consumed"}:
        raise _fail("方案尚未获整份授权或已失效")
    if (
        proposal.session_id != ctx.session.session_id
        or proposal.user_id != ctx.session.authenticated_user_id
    ):
        raise _fail("只能执行当前用户在本会话确认的方案", "P01")
    results = []
    for action in proposal.actions:
        if action.requires_approval:
            continue
        binding = {
            "proposal_id": proposal_id,
            "action_id": action.action_id,
            "version": version,
            "name": action.tool_name,
            "permission": "write",
            "execution_id": hashlib.sha256(
                canonical_json((ctx.session.session_id, proposal_id, action.action_id)).encode()
            ).hexdigest(),
        }
        if trace:
            trace.emit("tool_call", **binding, arguments=action.canonical_args)
        try:
            result = execute_action(
                ctx, proposal_id, action.action_id, version, action.tool_name, action.canonical_args
            )
        except Exception as exc:
            if trace:
                if isinstance(exc, PolicyError):
                    trace.emit(
                        "guard_verdict",
                        **binding,
                        allowed=False,
                        reason=str(exc),
                        rule_ids=exc.rule_ids,
                        source="execution",
                    )
                trace.emit("tool_result", **binding, is_error=True, error=type(exc).__name__)
            raise
        if trace:
            trace.emit(
                "tool_result", **binding, is_error=False, result=result.model_dump(mode="json")
            )
        results.append(result)
    return results

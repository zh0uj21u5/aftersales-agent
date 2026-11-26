"""Supervisor approval decisions and ownership-aware human work queues."""

import hashlib
import json
from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from aftersales.core.config import AgentConfig
from aftersales.core.execution import ActionExecution, canonical_json, persist_execution
from aftersales.core.loop import Session
from aftersales.core.outbox import enqueue
from aftersales.core.pg_sessions import PgSessionStore
from aftersales.domain.db import transaction
from aftersales.domain.services import PolicyError, Services
from aftersales.domain.settlement import notify
from aftersales.policy.guard import business_check
from aftersales.policy.proposal import business_arguments, validate_approval_arguments
from aftersales.tools.registry import ToolContext
from aftersales.web.security import Principal

if TYPE_CHECKING:
    from aftersales.web.production import Production


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    review_note: str = Field(min_length=1, max_length=2000)


class HandoffUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["claimed", "closed"]
    resolution: str | None = Field(default=None, min_length=1, max_length=2000)


def _review_approval(
    store: PgSessionStore, approval_id: str, review: Review, actor: Principal
) -> dict[str, Any]:
    db, clock = store.db, store.clock
    target = "approved" if review.decision == "approve" else "rejected"
    execution_key = f"approval:{approval_id}"
    with transaction(db):
        row = db.execute("SELECT * FROM approvals WHERE id=? FOR UPDATE", (approval_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "审批单不存在")
        if row["status"] == target:
            return {
                "id": approval_id,
                "status": target,
                "execution_key": execution_key if target == "approved" else None,
            }
        if row["status"] != "pending":
            raise HTTPException(409, "审批单已完成其他决定")
        record = None
        if target == "approved":
            order_id, action = row["order_id"], row["action"]
            args = validate_approval_arguments(
                action, json.loads(row["args"]), order_id, row["reason"]
            )
            skus = [args.new_sku_id] if action == "request_exchange" else []
            worker = Services(
                db,
                clock,
                execution_key=execution_key,
                approved_refund_cents=row["requested_amount_cents"],
            )
            with worker.lock_action_resources(order_id, skus, None):
                if worker.approved_refund_cents is None:
                    # Version 1 stored the request but did not persist a priced approval amount.
                    ctx = ToolContext(
                        Session(authenticated_user_id=row["user_id"]), AgentConfig(), db, clock
                    )
                    check = business_check(ctx, action, args, service=worker)
                    if not check.needs_approval or any(
                        issue.rule_id != "P08" for issue in check.violations
                    ):
                        raise HTTPException(409, "业务状态或金额已变化，请重新申请审批")
                    worker.approved_refund_cents = check.amount_cents
                before = worker.action_snapshot(order_id, sku_ids=skus)
                kwargs = {name: getattr(args, name) for name in type(args).model_fields}
                result = getattr(worker, action)(**kwargs)
            canonical = args.model_dump(mode="json")
            digest = hashlib.sha256(
                canonical_json(business_arguments(canonical)).encode()
            ).hexdigest()
            record = ActionExecution(
                execution_key=execution_key,
                session_id=row["session_id"],
                proposal_id="approval",
                action_id=approval_id,
                version=1,
                tool_name=action,
                canonical_args=canonical,
                args_hash=digest,
                result=result,
                before_snapshot=before,
                after_snapshot=worker.action_snapshot(order_id, sku_ids=skus),
                created_at=clock.now().isoformat(),
            )
        notice = (
            "已批准，原路退款处理中。"
            if target == "approved"
            else f"未批准。说明：{review.review_note}"
        )
        with db.pipeline():
            if record is not None:
                persist_execution(store, record)
            db.execute(
                """UPDATE approvals SET status=?, reviewer_id=?, review_note=?, reviewed_at=?
                   WHERE id=?""",
                (target, actor.user_id, review.review_note, clock.now().isoformat(), approval_id),
            )
            notify(db, clock, row["session_id"], execution_key, f"审批单 {approval_id} {notice}")
        if record is not None:
            enqueue(db, clock, execution_key, record.tool_name, record.result)
    return {
        "id": approval_id,
        "status": target,
        "execution_key": execution_key if target == "approved" else None,
    }


def router(production: "Production") -> APIRouter:
    routes = APIRouter(prefix="/api/admin")

    def supervisor(request: Request) -> Principal:
        actor: Principal = request.state.principal
        actor.require("supervisor", "admin")
        return actor

    @routes.get("/approvals")
    async def approvals(
        request: Request,
        status: Literal["pending", "approved", "rejected"] = "pending",
        after_id: str = "",
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        supervisor(request)

        def read(store: PgSessionStore) -> dict[str, Any]:
            rows = list(
                store.db.execute(
                    "SELECT * FROM approvals WHERE status=? AND id>? ORDER BY id LIMIT ?",
                    (status, after_id, limit + 1),
                )
            )
            service = Services(store.db, store.clock)
            snapshots = service.batch_action_snapshots([row["order_id"] for row in rows[:limit]])
            return {
                "items": [
                    {
                        **row,
                        "args": json.loads(row["args"]),
                        "rule_ids": ["P08"],
                        "snapshot": snapshot,
                    }
                    for row, snapshot in zip(rows[:limit], snapshots, strict=True)
                ],
                "next_after_id": rows[limit - 1]["id"] if len(rows) > limit else None,
            }

        return await production.run(read)

    @routes.post("/approvals/{approval_id}/decision")
    async def decision(approval_id: str, body: Review, request: Request) -> dict[str, Any]:
        actor = supervisor(request)
        try:
            return await production.run(
                lambda store: _review_approval(store, approval_id, body, actor)
            )
        except PolicyError as exc:
            raise HTTPException(409, {"message": str(exc), "rule_ids": exc.rule_ids}) from None
        except ValueError:
            raise HTTPException(409, "审批数据已失效，请重新提交") from None

    @routes.get("/handoffs")
    async def handoffs(
        request: Request,
        status: Literal["open", "claimed", "closed"] = "open",
        after_id: str = "",
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        supervisor(request)
        rows = await production.run(
            lambda store: list(
                store.db.execute(
                    "SELECT * FROM handoffs WHERE status=? AND id>? ORDER BY id LIMIT ?",
                    (status, after_id, limit + 1),
                )
            )
        )
        return {
            "items": rows[:limit],
            "next_after_id": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    @routes.patch("/handoffs/{handoff_id}")
    async def update_handoff(
        handoff_id: str, body: HandoffUpdate, request: Request
    ) -> dict[str, Any]:
        actor = supervisor(request)
        if body.status == "closed" and not body.resolution:
            raise HTTPException(422, "结单需要处理结果")

        def update(store: PgSessionStore) -> dict[str, Any]:
            db = store.db
            with transaction(db):
                row = db.execute(
                    "SELECT * FROM handoffs WHERE id=? FOR UPDATE", (handoff_id,)
                ).fetchone()
                if row is None:
                    raise HTTPException(404, "工单不存在")
                if row["assignee_id"] not in {None, actor.user_id} and actor.role != "admin":
                    raise HTTPException(409, "工单已由其他客服认领")
                if row["status"] == body.status:
                    return dict(row)
                if row["status"] == "closed" or (
                    body.status == "closed" and row["status"] != "claimed"
                ):
                    raise HTTPException(409, "工单状态不支持该操作")
                return dict(
                    db.execute(
                        """UPDATE handoffs SET status=?, assignee_id=?, closed_at=?, resolution=?
                       WHERE id=? RETURNING *""",
                        (
                            body.status,
                            row["assignee_id"] or actor.user_id,
                            store.clock.now().isoformat() if body.status == "closed" else None,
                            body.resolution,
                            handoff_id,
                        ),
                    ).fetchone()
                )

        return await production.run(update)

    return routes

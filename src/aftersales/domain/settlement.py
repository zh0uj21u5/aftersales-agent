"""Persist authoritative channel outcomes and retain the original refund reservation."""

from typing import Literal

from aftersales.domain.clock import Clock
from aftersales.domain.db import sequence_id_sql, transaction
from aftersales.domain.gateways import ChannelRefund
from aftersales.domain.storage import PgConnection

Outcome = Literal["applied", "unchanged", "mismatch", "conflict"]
Discrepancy = Literal["long_payment", "short_payment", "amount_mismatch", "terminal_conflict"]


def notify(db: PgConnection, clock: Clock, session_id: str | None, key: str, message: str) -> None:
    if session_id is not None:
        db.execute(
            """INSERT INTO session_notifications (session_id, notification_key, message, created_at)
               VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING""",
            (session_id, key, message, clock.now().isoformat()),
        )


def exception_handoff(db: PgConnection, clock: Clock, order_id: str, key: str, reason: str) -> None:
    db.execute(
        f"""INSERT INTO handoffs (id, user_id, summary, reason, created_at, dedup_key)
           SELECT {sequence_id_sql("handoffs")}, user_id, ?, ?, ?, ? FROM orders WHERE id=?
           ON CONFLICT (dedup_key) DO NOTHING""",
        (
            f"订单 {order_id} 渠道异常",
            reason,
            clock.now().isoformat(),
            key,
            order_id,
        ),
    )


def record_discrepancy(
    db: PgConnection,
    clock: Clock,
    kind: Discrepancy,
    refund_id: str,
    order_id: str | None,
    local_cents: int | None,
    channel_cents: int | None,
) -> bool:
    if order_id is not None:
        db.execute("SELECT id FROM orders WHERE id=? FOR UPDATE", (order_id,))
    changed = db.execute(
        """INSERT INTO reconciliation_discrepancies
               (kind, refund_id, order_id, local_cents, channel_cents, created_at)
           VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
        (kind, refund_id, order_id, local_cents, channel_cents, clock.now().isoformat()),
    ).rowcount
    if changed and order_id is not None:
        exception_handoff(
            db, clock, order_id, f"reconcile:{kind}:{refund_id}", "退款对账需人工复核"
        )
    return changed == 1


def apply_channel(db: PgConnection, clock: Clock, receipt: ChannelRefund) -> Outcome:
    """Accept repeated terminal results; flag conflicting terminal evidence."""
    with transaction(db):
        identity = db.execute(
            "SELECT order_id FROM refunds WHERE id=? AND execution_key IS NOT NULL",
            (receipt.refund_id,),
        ).fetchone()
        if identity is None:
            raise KeyError(receipt.refund_id)
        order_id = identity[0]
        db.execute("SELECT id FROM orders WHERE id=? FOR UPDATE", (order_id,))
        row = db.execute(
            """SELECT r.*, e.session_id FROM refunds r
               JOIN action_executions e USING (execution_key) WHERE r.id=? FOR UPDATE OF r""",
            (receipt.refund_id,),
        ).fetchone()
        assert row is not None
        if (row["order_id"], row["execution_key"]) != (receipt.order_id, receipt.execution_key):
            return "mismatch"
        if row["amount_cents"] != receipt.amount_cents:
            record_discrepancy(
                db,
                clock,
                "amount_mismatch",
                receipt.refund_id,
                order_id,
                row["amount_cents"],
                receipt.amount_cents,
            )
            return "mismatch"
        previous = row["status"]
        if previous == receipt.status or (
            previous in {"success", "failed"} and receipt.status == "processing"
        ):
            return "unchanged"
        if previous in {"success", "failed"}:
            record_discrepancy(
                db,
                clock,
                "terminal_conflict",
                receipt.refund_id,
                order_id,
                row["amount_cents"],
                receipt.amount_cents,
            )
            return "conflict"
        stamp = clock.now().isoformat()
        if previous == "pending":
            db.execute(
                """UPDATE refunds SET status='processing',
                     status_changed_at=greatest(?::timestamptz, status_changed_at, created_at)
                   WHERE id=?""",
                (stamp, receipt.refund_id),
            )
        if receipt.status == "processing":
            return "applied"
        db.execute(
            """UPDATE refunds SET status=?,
                 status_changed_at=greatest(?::timestamptz, status_changed_at, created_at)
               WHERE id=?""",
            (receipt.status, stamp, receipt.refund_id),
        )
        if row["aftersale_id"] is not None:
            db.execute(
                "UPDATE aftersales SET status=? WHERE id=? AND status='processing'",
                ("completed" if receipt.status == "success" else "failed", row["aftersale_id"]),
            )
        if receipt.status == "failed":
            exception_handoff(
                db, clock, order_id, f"refund:{receipt.refund_id}", "原路退款失败，额度保留"
            )
        message = (
            f"订单 {order_id} 的退款 {receipt.refund_id} 已原路退回。"
            if receipt.status == "success"
            else f"订单 {order_id} 的退款 {receipt.refund_id} 需人工处理。"
        )
        notify(db, clock, row["session_id"], f"refund:{receipt.refund_id}", message)
        return "applied"

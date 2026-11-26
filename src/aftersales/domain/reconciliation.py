"""Reconcile a complete channel statement and repair stale local refund outcomes."""

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel

from aftersales.domain.clock import Clock
from aftersales.domain.db import transaction
from aftersales.domain.gateways import ChannelRefund, PaymentGateway
from aftersales.domain.settlement import Discrepancy, apply_channel, record_discrepancy
from aftersales.domain.storage import PgConnection, PostgresStorage


class LocalRefund(BaseModel):
    id: str
    order_id: str
    execution_key: str
    amount_cents: int
    status: Literal["pending", "processing", "success", "failed"]
    changed_at: datetime


class ReconciliationReport(BaseModel):
    queried: int = 0
    repaired: int = 0
    unknown: int = 0
    long_payment: int = 0
    short_payment: int = 0
    amount_mismatch: int = 0
    terminal_conflict: int = 0


def _refunds(db: PgConnection, ids: list[str] | None = None) -> list[LocalRefund]:
    clause, params = ("AND id=ANY(?::text[])", (ids,)) if ids is not None else ("", ())
    return [
        LocalRefund.model_validate(dict(row))
        for row in db.execute(
            f"""SELECT id, order_id, execution_key, amount_cents, status,
              coalesce(status_changed_at, created_at) AS changed_at
              FROM refunds WHERE execution_key IS NOT NULL AND amount_cents>0 {clause}
              ORDER BY id""",
            params,
        )
    ]


def _compare(
    db: PgConnection,
    clock: Clock,
    as_of: datetime,
    entries: dict[str, ChannelRefund],
    report: ReconciliationReport,
) -> None:
    local = {item.id: item for item in _refunds(db)}
    suspect = sorted(
        refund_id
        for refund_id in local.keys() | entries.keys()
        if _difference(local.get(refund_id), entries.get(refund_id), as_of) is not None
    )
    if not suspect:
        return
    order_ids = sorted(
        {
            local[refund_id].order_id if refund_id in local else entries[refund_id].order_id
            for refund_id in suspect
        }
    )
    with transaction(db):
        # Healthy orders need no lock. Lock suspect orders in key order and recheck.
        known = {
            row[0]
            for row in db.execute(
                "SELECT id FROM orders WHERE id=ANY(?::text[]) ORDER BY id FOR UPDATE",
                (order_ids,),
            )
        }
        local = {item.id: item for item in _refunds(db, suspect)}
        for refund_id in suspect:
            row = local.get(refund_id)
            paid = entries.get(refund_id)
            kind = _difference(row, paid, as_of)
            if kind is None:
                continue
            order_id = row.order_id if row is not None else (paid.order_id if paid else None)
            record_discrepancy(
                db,
                clock,
                kind,
                refund_id,
                order_id if order_id in known else None,
                row.amount_cents if row else None,
                paid.amount_cents if paid else None,
            )
            setattr(report, kind, getattr(report, kind) + 1)


def _difference(
    row: LocalRefund | None, paid: ChannelRefund | None, as_of: datetime
) -> Discrepancy | None:
    if paid is None:
        return (
            "short_payment" if row and row.status == "success" and row.changed_at <= as_of else None
        )
    if row is None:
        return "long_payment"
    if (row.order_id, row.execution_key) != (paid.order_id, paid.execution_key):
        return "terminal_conflict"
    if row.status == "failed" and row.changed_at <= as_of:
        return "long_payment"
    if row.amount_cents != paid.amount_cents:
        return "amount_mismatch"
    return None


async def reconcile(
    storage: PostgresStorage,
    clock: Clock,
    payment: PaymentGateway,
    *,
    hanging_age: timedelta = timedelta(minutes=5),
    statement_lag: timedelta = timedelta(minutes=5),
) -> ReconciliationReport:
    report = ReconciliationReport()
    now = clock.now()
    for item in await storage.run(_refunds):
        if item.status not in {"pending", "processing"} or item.changed_at > now - hanging_age:
            continue
        report.queried += 1
        receipt = await payment.lookup(item.id)
        if receipt is None or receipt.status == "processing":
            report.unknown += 1
            continue
        if receipt.refund_id != item.id:
            raise ValueError("渠道查询返回了不同退款编号")
        outcome = await storage.run(lambda db, receipt=receipt: apply_channel(db, clock, receipt))
        report.repaired += outcome == "applied"
    as_of = now - statement_lag
    statement = await payment.statement(as_of)
    entries = {item.refund_id: item for item in statement.refunds}
    if (
        not statement.complete
        or statement.as_of != as_of
        or len(entries) != len(statement.refunds)
        or any(item.status != "success" for item in entries.values())
    ):
        raise ValueError("渠道账单不完整，无法判定资金差异")
    await storage.run(lambda db: _compare(db, clock, as_of, entries, report))
    return report

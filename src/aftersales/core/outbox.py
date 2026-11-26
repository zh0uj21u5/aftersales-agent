"""Durable asynchronous dispatch with leased claims and stable channel keys."""

import asyncio
import logging
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from aftersales.domain.clock import Clock
from aftersales.domain.db import transaction
from aftersales.domain.gateways import (
    ChannelRefund,
    FulfillmentGateway,
    GatewayRejected,
    PaymentGateway,
    RefundInstruction,
)
from aftersales.domain.models import Address, OperationResult
from aftersales.domain.services import Services
from aftersales.domain.settlement import apply_channel, exception_handoff, notify
from aftersales.domain.storage import PgConnection, PostgresStorage

LOGGER = logging.getLogger(__name__)
MAX_ATTEMPTS = 8
LEASE_SECONDS = 90
DISPATCH_TIMEOUT = 20
Topic = Literal["refund", "cancel_order", "modify_address"]


class Payload(BaseModel):
    refund_ids: list[str] = Field(default_factory=list)
    address: Address | None = None


class Event(BaseModel):
    id: int
    execution_key: str
    order_id: str
    topic: Topic
    payload: Payload
    attempts: int
    lease_token: str
    fulfillment_done: bool


def enqueue(
    db: PgConnection,
    clock: Clock,
    execution_key: str,
    tool_name: str,
    result: OperationResult,
    *,
    address: Address | None = None,
) -> None:
    if result.status != "processing":
        return
    topic = tool_name if tool_name in {"cancel_order", "modify_address"} else "refund"
    stamp = clock.now().isoformat()
    inserted = db.execute(
        """INSERT INTO outbox_events
           (execution_key, order_id, topic, payload_json, next_retry_at, created_at)
           SELECT ?, ?, ?,
             jsonb_build_object('refund_ids', coalesce(jsonb_agg(id ORDER BY id), '[]'),
                                'address', ?::jsonb), ?, ?
           FROM refunds WHERE execution_key=? AND status='pending'
           HAVING count(*)>0 OR ? IN ('cancel_order','modify_address')""",
        (
            execution_key,
            result.order_id,
            topic,
            address.model_dump_json() if address else None,
            stamp,
            stamp,
            execution_key,
            topic,
        ),
    ).rowcount
    if inserted != 1:
        raise RuntimeError("A processing action requires an outbox event")


class OutboxRelay:
    def __init__(
        self,
        storage: PostgresStorage,
        clock: Clock,
        payment: PaymentGateway,
        fulfillment: FulfillmentGateway,
        *,
        batch_size: int = 8,
        max_attempts: int = MAX_ATTEMPTS,
    ) -> None:
        if not 1 <= batch_size <= 50 or max_attempts < 1:
            raise ValueError("Invalid outbox worker capacity")
        self.storage, self.clock, self.payment, self.fulfillment = (
            storage,
            clock,
            payment,
            fulfillment,
        )
        self.batch_size, self.max_attempts = batch_size, max_attempts

    def _claim(self, db: PgConnection) -> list[Event]:
        now = self.clock.now()
        stamp = now.isoformat()
        expires = (now + timedelta(seconds=LEASE_SECONDS)).isoformat()
        claimed = []
        with transaction(db):
            rows = db.execute(
                """SELECT e.* FROM outbox_events e
                  WHERE ((e.status='pending' AND e.next_retry_at<=?)
                      OR (e.status='processing' AND e.lease_until<=?))
                    AND (e.topic='refund' OR NOT EXISTS (
                        SELECT 1 FROM outbox_events older WHERE older.order_id=e.order_id
                        AND older.id<e.id AND older.topic IN ('cancel_order','modify_address')
                        AND older.status<>'done'))
                  ORDER BY e.id LIMIT ? FOR UPDATE OF e SKIP LOCKED""",
                (stamp, stamp, self.batch_size),
            ).fetchall()
            for row in rows:
                token = uuid4().hex
                attempts = row["attempts"] + 1
                if attempts > self.max_attempts:
                    self._mark_dead(
                        db, row["id"], row["order_id"], row["execution_key"], "LeaseExpired"
                    )
                    continue
                db.execute(
                    """UPDATE outbox_events SET status='processing', attempts=?,
                           lease_token=?, lease_until=? WHERE id=?""",
                    (attempts, token, expires, row["id"]),
                )
                claimed.append(
                    Event(
                        id=row["id"],
                        execution_key=row["execution_key"],
                        order_id=row["order_id"],
                        topic=row["topic"],
                        payload=Payload.model_validate_json(row["payload_json"]),
                        attempts=attempts,
                        lease_token=token,
                        fulfillment_done=row["fulfillment_done_at"] is not None,
                    )
                )
        return claimed

    def _owned(self, db: PgConnection, event: Event) -> bool:
        return (
            db.execute(
                """SELECT 1 FROM outbox_events WHERE id=? AND status='processing'
               AND lease_token=? AND lease_until>? FOR UPDATE""",
                (event.id, event.lease_token, self.clock.now().isoformat()),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _check_reconciliation(db: PgConnection, event: Event) -> None:
        if db.execute(
            """SELECT 1 FROM reconciliation_discrepancies
               WHERE order_id=? AND resolved_at IS NULL LIMIT 1""",
            (event.order_id,),
        ).fetchone():
            raise GatewayRejected("订单已进入人工资金核对")

    def _fulfillment_allowed(self, db: PgConnection, event: Event) -> bool:
        with transaction(db):
            db.execute("SELECT id FROM orders WHERE id=? FOR UPDATE", (event.order_id,))
            if not self._owned(db, event):
                return False
            self._check_reconciliation(db, event)
            return True

    def _mark_dead(
        self, db: PgConnection, event_id: int, order_id: str, key: str, error: str
    ) -> None:
        db.execute(
            """UPDATE outbox_events SET status='dead_letter', lease_token=NULL, lease_until=NULL,
               last_error=? WHERE id=?""",
            (error, event_id),
        )
        exception_handoff(db, self.clock, order_id, f"outbox:{key}", "外部执行未确认，请核实渠道")

    def _failure(self, db: PgConnection, event: Event, error: Exception) -> None:
        error_name = type(error).__name__
        with transaction(db):
            if not self._owned(db, event):
                return
            if event.attempts >= self.max_attempts or isinstance(error, GatewayRejected):
                self._mark_dead(db, event.id, event.order_id, event.execution_key, error_name)
            else:
                delay = min(300, 2 ** (event.attempts - 1))
                retry_at = (self.clock.now() + timedelta(seconds=delay)).isoformat()
                db.execute(
                    """UPDATE outbox_events SET status='pending', lease_token=NULL,
                       lease_until=NULL, last_error=?, next_retry_at=? WHERE id=?""",
                    (error_name, retry_at, event.id),
                )

    def _finalize_fulfillment(self, db: PgConnection, event: Event) -> bool:
        with transaction(db):
            db.execute("SELECT id FROM orders WHERE id=? FOR UPDATE", (event.order_id,))
            if not self._owned(db, event):
                return False
            if event.topic == "cancel_order":
                service = Services(db, self.clock)
                service.release_cancel_stock(service.get_order(event.order_id))
            db.execute(
                "UPDATE outbox_events SET fulfillment_done_at=? WHERE id=?",
                (self.clock.now().isoformat(), event.id),
            )
            session = db.execute(
                "SELECT session_id FROM action_executions WHERE execution_key=?",
                (event.execution_key,),
            ).fetchone()
            note = "仓库已确认拦截与取消" if event.topic == "cancel_order" else "仓库已确认地址同步"
            notify(
                db,
                self.clock,
                session[0],
                f"fulfillment:{event.execution_key}",
                f"订单 {event.order_id} {note}。",
            )
            return True

    def _instructions(self, db: PgConnection, event: Event) -> list[RefundInstruction]:
        if not event.payload.refund_ids:
            return []
        with transaction(db):
            db.execute("SELECT id FROM orders WHERE id=? FOR UPDATE", (event.order_id,))
            if not self._owned(db, event):
                return []
            rows = db.execute(
                """SELECT id AS refund_id, order_id, amount_cents, payment_method_id, status
                   FROM refunds WHERE id=ANY(?::text[]) AND execution_key=? ORDER BY id""",
                (event.payload.refund_ids, event.execution_key),
            ).fetchall()
            if len(rows) != len(event.payload.refund_ids):
                raise GatewayRejected("退款批次记录不完整")
            if all(item["status"] in {"success", "failed"} for item in rows):
                return []
            self._check_reconciliation(db, event)
            db.execute(
                """UPDATE refunds SET status='processing',
                    status_changed_at=greatest(?::timestamptz, status_changed_at, created_at)
                    WHERE execution_key=? AND status='pending'""",
                (self.clock.now().isoformat(), event.execution_key),
            )
            return [
                RefundInstruction.model_validate({k: v for k, v in row.items() if k != "status"})
                for row in rows
            ]

    def _complete(self, db: PgConnection, event: Event, results: list[ChannelRefund]) -> None:
        with transaction(db):
            db.execute("SELECT id FROM orders WHERE id=? FOR UPDATE", (event.order_id,))
            if not self._owned(db, event):
                return
            dispositions = [apply_channel(db, self.clock, item) for item in results]
            if any(outcome in {"conflict", "mismatch"} for outcome in dispositions):
                self._mark_dead(
                    db, event.id, event.order_id, event.execution_key, "ChannelConflict"
                )
                return
            db.execute(
                """UPDATE outbox_events SET status='done', lease_token=NULL, lease_until=NULL,
                   last_error=NULL WHERE id=?""",
                (event.id,),
            )

    async def _deliver(self, event: Event) -> None:
        try:
            async with asyncio.timeout(DISPATCH_TIMEOUT):
                if event.topic != "refund" and not event.fulfillment_done:
                    if not await self.storage.run(lambda db: self._fulfillment_allowed(db, event)):
                        return
                    if event.topic == "cancel_order":
                        await self.fulfillment.intercept(event.execution_key, event.order_id)
                    else:
                        if event.payload.address is None:
                            raise GatewayRejected("缺少待同步地址")
                        await self.fulfillment.change_address(
                            event.execution_key, event.order_id, event.payload.address
                        )
                    if not await self.storage.run(lambda db: self._finalize_fulfillment(db, event)):
                        return
                instructions = await self.storage.run(lambda db: self._instructions(db, event))
                receipts = (
                    await self.payment.refund(event.execution_key, instructions)
                    if instructions
                    else []
                )
                expected = {item.refund_id: item.order_id for item in instructions}
                if (
                    len(receipts) != len(instructions)
                    or {
                        item.refund_id: item.order_id
                        for item in receipts
                        if item.execution_key == event.execution_key
                    }
                    != expected
                ):
                    raise GatewayRejected("渠道回执与退款批次不匹配")
                await self.storage.run(lambda db: self._complete(db, event, receipts))
        except Exception as exc:  # noqa: BLE001 - external or local errors leave a durable retry
            error = exc
            await self.storage.run(lambda db: self._failure(db, event, error))

    async def tick(self) -> int:
        claimed = await self.storage.run(self._claim)
        outcomes = await asyncio.gather(
            *(self._deliver(event) for event in claimed), return_exceptions=True
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                LOGGER.warning(
                    "Outbox claim will recover after lease expiry (%s)", type(outcome).__name__
                )
        return len(claimed)

    async def run(self, stopping: asyncio.Event) -> None:
        while not stopping.is_set():
            try:
                if await self.tick():
                    continue
            except Exception as exc:  # noqa: BLE001 - recover after a database outage
                LOGGER.warning("Outbox polling failed (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(stopping.wait(), timeout=0.25)
            except TimeoutError:
                pass

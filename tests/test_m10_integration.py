"""M10 acceptance with genuine PG/Redis and controlled external channel faults."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from test_production_integration import CLOCK, MODEL, PRICE_ARGS, Infra, auth, tool, with_infra

from aftersales.core.config import AgentConfig
from aftersales.core.llm import AsyncMockLLM, LLMResponse
from aftersales.web.app import create_app

if TYPE_CHECKING:
    from aftersales.core.llm import Client
    from aftersales.domain.gateways import FulfillmentGateway, PaymentGateway
    from aftersales.domain.storage import PgConnection

pytestmark = pytest.mark.integration
WEBHOOK_SECRET = b"M10 tests only 32 byte payment callback key"
OPTIONS = AgentConfig(proposal_gate=True, model=MODEL)


@dataclass
class AdvancingClock:
    value: datetime = field(default_factory=CLOCK.now)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@asynccontextmanager
async def gateway(
    infra: Infra,
    clock: AdvancingClock,
    llm: Client,
    *,
    payment: PaymentGateway | None = None,
    fulfillment: FulfillmentGateway | None = None,
    **kwargs: Any,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        None,
        llm,
        OPTIONS,
        clock=clock,
        production=replace(infra.settings(), webhook_secret=WEBHOOK_SECRET),
        payment_gateway=payment,
        fulfillment_gateway=fulfillment,
        **kwargs,
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m10") as client,
    ):
        yield client


def proposal_model(name: str = "apply_price_protection", **args: Any) -> AsyncMockLLM:
    return AsyncMockLLM(
        [
            tool("propose_actions", actions=[{"tool_name": name, "arguments": args}]),
            LLMResponse(text="请查看并确认方案。"),
        ]
    )


async def approve_proposal(client: httpx.AsyncClient, user: str = "U006") -> tuple[str, dict]:
    headers = auth(user)
    created = await client.post("/api/sessions", headers=headers)
    assert created.status_code == 201, created.text
    path = "/api/sessions/" + created.json()["session_id"]
    proposed = await client.post(
        path + "/messages", headers=headers, json={"content": "请办理售后"}
    )
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["session"]["proposals"], proposed.text
    proposal = proposed.json()["session"]["proposals"][-1]
    action = f"{path}/proposals/{proposal['proposal_id']}/decision"
    response = await client.post(
        action,
        headers=headers,
        json={"version": proposal["version"], "decision": "approve"},
    )
    assert response.status_code == 200, response.text
    return path, response.json()["action_executions"][-1]


def test_additive_v1_migration_preserves_live_rows() -> None:
    from aftersales.core.loop import Session
    from aftersales.core.pg_sessions import PgSessionStore
    from aftersales.domain.migrate import migrate

    async def check(infra: Infra) -> None:
        session = Session(authenticated_user_id="U001")
        session._owner_user_id = "U001"
        await infra.db.run(lambda db: PgSessionStore(db, CLOCK).save(session))

        def legacy(db: PgConnection) -> tuple[list, list, int]:
            return (
                [
                    tuple(row.values())
                    for row in db.execute(
                        "SELECT id, amount_cents, status, payment_method_id FROM refunds ORDER BY id"
                    )
                ],
                [
                    tuple(row.values())
                    for row in db.execute(
                        "SELECT order_id, amount_cents FROM order_refund_totals ORDER BY order_id"
                    )
                ],
                PgSessionStore(db, CLOCK).load(session.session_id)._revision,
            )

        assert (
            await infra.db.run(
                lambda db: db.execute("SELECT to_regclass('outbox_events')").fetchone()[0]
            )
            is None
        )
        before = await infra.db.run(legacy)
        await infra.db.run(migrate)
        await infra.db.run(migrate)
        assert await infra.db.run(legacy) == before
        assert await infra.db.run(
            lambda db: [
                row[0]
                for row in db.execute("SELECT version FROM schema_migrations ORDER BY version")
            ]
        ) == [1, 2]

    with_infra(check, migration_target=1)


def test_exchange_row_and_partial_refund_guard_in_postgres() -> None:
    from aftersales.domain.models import ReturnItem
    from aftersales.domain.services import PolicyError, Services

    async def check(infra: Infra) -> None:
        def prepare(db: PgConnection) -> None:
            db.execute(
                "UPDATE orders SET items_total_cents=26000,paid_cents=26000 WHERE id='O1002'"
            )
            db.execute("UPDATE order_items SET qty=2,paid_share_cents=26000 WHERE id='OI00002'")
            service = Services(db, CLOCK)
            # Half of the 26,000-cent line uses 13,000 cents. A 13,001-cent write must fail.
            result = service.request_return(
                "O1002",
                [ReturnItem(order_item_id="OI00002", qty=1)],
                "部分退货",
            )
            assert result.amount_cents == 13000
            with pytest.raises(PolicyError) as budget:
                service._reserve_line_refund("OI00002", 13001)
            assert budget.value.rule_ids == ["P06"]
            with pytest.raises(PolicyError) as exchanged:
                service.request_exchange("O1002", "OI00002", "S0002", "部分退货后换货")
            assert "P09" in exchanged.value.rule_ids
            assert service.get_line("O1002", "OI00002").item.refunded_cents == 13000
            snapshot = service.action_snapshot("O1002")
            with pytest.MonkeyPatch.context() as patch:
                statements = []
                execute = db.execute

                def count(sql: str, params: tuple[Any, ...] = ()) -> Any:
                    statements.append(sql)
                    return execute(sql, params)

                patch.setattr(db, "execute", count)
                missing, first, duplicate = service.batch_action_snapshots(
                    ["missing", "O1002", "O1002"]
                )
                assert service.batch_action_snapshots([]) == []
                assert len(statements) == 1
            assert missing.model_dump() == {name: [] for name in type(missing).model_fields}
            assert snapshot == first == duplicate
            assert snapshot.order_items[0]["refunded_cents"] == 13000
            extra = service.action_snapshot(None, sku_ids=["S0002"], user_coupon_id="UC0001")
            assert [sku["id"] for sku in extra.skus] == ["S0002"]
            assert [coupon["id"] for coupon in extra.user_coupons] == ["UC0001"]

            db.execute(
                "UPDATE orders SET items_total_cents=42000,paid_cents=42000 WHERE id='O1002'"
            )
            db.execute(
                """INSERT INTO order_items (id, order_id, sku_id, qty, unit_price_cents,
                                            paid_share_cents, status)
                   SELECT 'OI99999', order_id, sku_id, 1, 16000, 16000, 'normal'
                   FROM order_items WHERE id='OI00002'"""
            )
            db.execute("SELECT setval('aftersales_id_seq', 9999)")
            # The remaining 13,000 cents and the new 16,000-cent line require P08 approval.
            worker = Services(db, CLOCK, approved_refund_cents=29000)
            items = [ReturnItem(order_item_id=item, qty=1) for item in ("OI99999", "OI00002")]
            before = service.action_snapshot("O1002")
            db.execute(
                """CREATE FUNCTION suppress_return() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN IF NEW.id='OI00002' THEN RETURN NULL; END IF; RETURN NEW; END; $$"""
            )
            db.execute(
                """CREATE TRIGGER suppress_return BEFORE UPDATE ON order_items
                   FOR EACH ROW EXECUTE FUNCTION suppress_return()"""
            )
            with pytest.raises(PolicyError) as conflict:
                worker.request_return("O1002", items, "多行退货")
            assert conflict.value.rule_ids == ["P05"]
            assert service.action_snapshot("O1002") == before
            assert service.refunded_cents("O1002") == 13000
            db.execute("DROP TRIGGER suppress_return ON order_items")
            result = worker.request_return("O1002", items, "多行退货")
            assert result.amount_cents == 29000
            assert service.refunded_cents("O1002") == 42000
            after = service.action_snapshot("O1002")
            sales = {row["id"]: row for row in after.aftersales}
            refunds = {row["id"]: row for row in after.refunds}
            for item, sale_id, refund_id, amount in zip(
                items, result.record_ids[::2], result.record_ids[1::2], (16000, 13000), strict=True
            ):
                assert sale_id.startswith("AS100")
                assert sales[sale_id]["order_item_id"] == item.order_item_id
                refund = refunds[refund_id]
                assert (refund["aftersale_id"], refund["amount_cents"], refund["status"]) == (
                    sale_id,
                    amount,
                    "success",
                )

        await infra.db.run(prepare)

    with_infra(check)


async def callback(
    client: httpx.AsyncClient,
    clock: AdvancingClock,
    event: dict,
    *,
    skew: int = 0,
    good_signature: bool = True,
) -> httpx.Response:
    from aftersales.web.webhooks import sign_payment_event

    stamp = str(int(clock.now().timestamp()) + skew)
    body = json.dumps(event, sort_keys=True).encode()
    signature = sign_payment_event(WEBHOOK_SECRET, stamp, body) if good_signature else "0" * 64
    return await client.post(
        "/api/webhooks/payment",
        content=body,
        headers={"X-Timestamp": stamp, "X-Signature": signature},
    )


def test_worker_retry_and_signed_callbacks_do_not_pay_twice() -> None:
    from aftersales.core.outbox import OutboxRelay
    from aftersales.domain.gateways import SandboxFulfillment, SandboxPayment

    async def check(infra: Infra) -> None:
        clock = AdvancingClock()
        payment = SandboxPayment(clock, ("timeout", "lost_response"))
        fulfillment = SandboxFulfillment()
        first = OutboxRelay(infra.db, clock, payment, fulfillment)
        second = OutboxRelay(infra.db, clock, payment, fulfillment)
        async with gateway(infra, clock, proposal_model(**PRICE_ARGS)) as client:
            path, execution = await approve_proposal(client)
            key = execution["execution_key"]
            assert execution["result"]["status"] == "processing"
            assert not payment.attempts
            # A 6,000-cent nominal drop at a 24,700/26,000 paid share returns 5,700 cents.
            for delay in (1, 2, 0):
                assert sum(await asyncio.gather(first.tick(), second.tick())) == 1
                clock.advance(delay)
            assert payment.attempts == [key, key, key]
            assert len(payment.envelopes) == len(payment.refunds) == 1
            paid = next(iter(payment.refunds.values()))
            event = {**paid.model_dump(), "event_id": "evt-final"}
            assert (await callback(client, clock, event, skew=-301)).status_code == 401
            assert (await callback(client, clock, event, good_signature=False)).status_code == 401
            late = {**event, "status": "processing", "event_id": "evt-late"}
            for item, expected in [
                (event, "unchanged"),
                (late, "unchanged"),
                (event, "duplicate"),
                (late, "duplicate"),
                ({**event, "event_id": "evt-repeat"}, "unchanged"),
            ]:
                assert (await callback(client, clock, item)).json() == {"status": expected}
            assert (await callback(client, clock, {**event, "status": "failed"})).status_code == 409
            outcome = await infra.db.run(
                lambda db: tuple(
                    db.execute(
                        """SELECT count(*), sum(amount_cents), min(status) FROM refunds
                   WHERE execution_key=?""",
                        (key,),
                    )
                    .fetchone()
                    .values()
                )
            )
            assert outcome == (1, 5700, "success")
            state = (await client.get(path, headers=auth("U006"))).json()
            assert len(state["notifications"]) == 1
            assert "已原路退回" in state["notifications"][0]["message"]

    with_infra(check)


def test_failed_callback_retains_both_quotas_and_opens_one_handoff() -> None:
    from psycopg.errors import CheckViolation

    async def check(infra: Infra) -> None:
        clock = AdvancingClock()
        async with gateway(infra, clock, proposal_model(**PRICE_ARGS)) as client:
            _, execution = await approve_proposal(client)
            key = execution["execution_key"]
            refund_id = execution["result"]["record_ids"][-1]
            event = {
                "event_id": "failed-1",
                "refund_id": refund_id,
                "order_id": "O1006",
                "execution_key": key,
                "status": "failed",
                "amount_cents": 5700,
            }
            assert (await callback(client, clock, event)).json()["status"] == "applied"
            assert (await callback(client, clock, {**event, "event_id": "failed-2"})).json()[
                "status"
            ] == "unchanged"
            snapshot = await infra.db.run(
                lambda db: tuple(
                    db.execute(
                        """SELECT r.status, t.amount_cents, i.refunded_cents, count(h.id)
                   FROM refunds r JOIN order_refund_totals t USING (order_id)
                   JOIN aftersales a ON a.id=r.aftersale_id
                   JOIN order_items i ON i.id=a.order_item_id
                   LEFT JOIN handoffs h ON h.dedup_key='refund:' || r.id
                   WHERE r.id=? GROUP BY r.status, t.amount_cents, i.refunded_cents""",
                        (refund_id,),
                    )
                    .fetchone()
                    .values()
                )
            )
            # Failed 5,700-cent refund keeps both the order reservation and its line reservation.
            assert snapshot == ("failed", 5700, 5700, 1)
            for sql in (
                "DELETE FROM refunds WHERE id=?",
                "UPDATE refunds SET amount_cents=1 WHERE id=?",
                "UPDATE refunds SET status='pending' WHERE id=?",
            ):
                with pytest.raises(CheckViolation):
                    await infra.db.run(lambda db, sql=sql: db.execute(sql, (refund_id,)))

    with_infra(check)


def test_reclaimed_lease_dead_letters_after_retry_limit() -> None:
    from aftersales.core.outbox import OutboxRelay
    from aftersales.domain.gateways import SandboxFulfillment, SandboxPayment

    async def check(infra: Infra) -> None:
        clock = AdvancingClock()
        payment = SandboxPayment(clock, ("timeout",))
        async with gateway(infra, clock, proposal_model(**PRICE_ARGS)) as client:
            _, execution = await approve_proposal(client)
        key = execution["execution_key"]
        await infra.db.run(
            lambda db: db.execute(
                """UPDATE outbox_events SET status='processing', attempts=1,
                   lease_token='abandoned', lease_until=? WHERE execution_key=?""",
                (clock.now().isoformat(), key),
            )
        )
        relay = OutboxRelay(infra.db, clock, payment, SandboxFulfillment(), max_attempts=2)
        clock.advance(1)
        assert await relay.tick() == 1 and await relay.tick() == 0
        assert payment.attempts == [key]
        assert await infra.db.run(
            lambda db: tuple(
                db.execute(
                    """SELECT o.status AS outbox_status, o.attempts,
                       r.status AS refund_status, r.amount_cents, h.status AS handoff_status
               FROM outbox_events o JOIN refunds r USING (execution_key)
               JOIN handoffs h ON h.dedup_key='outbox:' || o.execution_key
               WHERE o.execution_key=?""",
                    (key,),
                )
                .fetchone()
                .values()
            )
        ) == ("dead_letter", 2, "processing", 5700, "open")
        async with gateway(
            infra, clock, AsyncMockLLM([]), payment=payment, fulfillment=SandboxFulfillment()
        ) as client:
            ready = await client.get("/readyz")
            assert ready.status_code == 503
            assert ready.json()["checks"]["outbox_dead_letters"] == 1

    with_infra(check)


def test_open_reconciliation_difference_blocks_queued_dispatch() -> None:
    from aftersales.core.outbox import OutboxRelay
    from aftersales.domain.gateways import SandboxFulfillment, SandboxPayment
    from aftersales.domain.settlement import record_discrepancy

    async def check(infra: Infra) -> None:
        clock = AdvancingClock()
        payment = SandboxPayment(clock)
        async with gateway(infra, clock, proposal_model(**PRICE_ARGS)) as client:
            _, execution = await approve_proposal(client)
        key = execution["execution_key"]
        await infra.db.run(
            lambda db: record_discrepancy(
                db,
                clock,
                "long_payment",
                "R-other",
                "O1006",
                None,
                5,
            )
        )
        relay = OutboxRelay(infra.db, clock, payment, SandboxFulfillment())
        assert await relay.tick() == 1
        assert not payment.attempts
        assert (
            await infra.db.run(
                lambda db: db.execute(
                    "SELECT status FROM outbox_events WHERE execution_key=?",
                    (key,),
                ).fetchone()[0]
            )
            == "dead_letter"
        )

    with_infra(check)


def test_250_yuan_approval_supervisor_and_handoff_lifecycle() -> None:
    from aftersales.core.outbox import OutboxRelay
    from aftersales.core.pg_sessions import PgSessionStore
    from aftersales.domain.gateways import SandboxFulfillment, SandboxPayment
    from aftersales.domain.services import PolicyError, Services
    from aftersales.web.admin import Review, _review_approval
    from aftersales.web.security import Principal

    async def check(infra: Infra) -> None:
        clock = AdvancingClock()
        payment = SandboxPayment(clock)
        # Quantity 1 × 25,000 cents, no coupon. Full return is 25,000 cents and exceeds P08.
        await infra.db.run(
            lambda db: db.execute(
                "UPDATE orders SET items_total_cents=25000,paid_cents=25000 WHERE id='O1005'"
            )
        )
        await infra.db.run(
            lambda db: db.execute(
                """UPDATE order_items SET qty=1,unit_price_cents=25000,paid_share_cents=25000
                WHERE id='OI00005'"""
            )
        )
        args = {
            "order_id": "O1005",
            "items": [{"order_item_id": "OI00005", "qty": 1}],
            "reason": "退货",
        }
        llm = AsyncMockLLM(
            [
                tool("request_return", **args),
                tool(
                    "submit_approval",
                    order_id="O1005",
                    action="request_return",
                    args={"items": args["items"]},
                    reason="超限",
                ),
                LLMResponse(text="审批已提交。"),
            ]
        )
        async with gateway(infra, clock, llm) as client:
            buyer, supervisor = auth("U005"), auth("S01", "supervisor")
            created = await client.post("/api/sessions", headers=buyer)
            path = "/api/sessions/" + created.json()["session_id"]
            turn = await client.post(
                path + "/messages", headers=buyer, json={"content": "请整单退货"}
            )
            assert turn.status_code == 200, turn.text
            state = turn.json()["session"]
            assert any(event["data"].get("rule_ids") == ["P08"] for event in state["trace_events"])
            approval_id = state["approvals"][0]["id"]
            decision = f"/api/admin/approvals/{approval_id}/decision"
            body = {"decision": "approve", "review_note": "复核通过"}
            assert (await client.get("/api/admin/approvals", headers=buyer)).status_code == 403
            assert (
                await client.get("/api/admin/approvals?status=invalid", headers=buyer)
            ).status_code == 403
            assert (await client.post(decision, headers=buyer, json=body)).status_code == 403
            assert (await client.get("/api/admin/handoffs", headers=buyer)).status_code == 403
            listing = (await client.get("/api/admin/approvals", headers=supervisor)).json()
            assert listing["items"][0]["requested_amount_cents"] == 25000
            # A revised 19,000-cent amount is below P08 and does not match the submitted 25,000.
            await infra.db.run(
                lambda db: db.execute(
                    "UPDATE order_items SET paid_share_cents=19000 WHERE id='OI00005'"
                )
            )
            assert (await client.post(decision, headers=supervisor, json=body)).status_code == 409
            assert (
                await infra.db.run(
                    lambda db: db.execute("SELECT count(*) FROM outbox_events").fetchone()[0]
                )
                == 0
            )
            await infra.db.run(
                lambda db: db.execute(
                    "UPDATE order_items SET paid_share_cents=25000 WHERE id='OI00005'"
                )
            )

            def failed_outbox(db: PgConnection) -> None:
                service = Services(db, clock)
                before = service.action_snapshot("O1005")
                reviewer = Principal(user_id="S01", role="supervisor")
                review = Review(**body)
                db.execute(
                    """INSERT INTO reconciliation_discrepancies (kind, refund_id, order_id, created_at)
                       VALUES ('amount_mismatch', 'perf-test', 'O1005', ?)""",
                    (clock.now().isoformat(),),
                )
                with pytest.raises(PolicyError) as blocked:
                    _review_approval(PgSessionStore(db, clock), approval_id, review, reviewer)
                assert blocked.value.rule_ids == ["P12"]
                db.execute(
                    "UPDATE reconciliation_discrepancies SET resolved_at=? WHERE refund_id='perf-test'",
                    (clock.now().isoformat(),),
                )
                db.execute(
                    """CREATE FUNCTION suppress_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
                       BEGIN RETURN NULL; END; $$"""
                )
                db.execute(
                    """CREATE TRIGGER suppress_outbox BEFORE INSERT ON outbox_events
                       FOR EACH ROW EXECUTE FUNCTION suppress_outbox()"""
                )
                with pytest.raises(RuntimeError, match="requires an outbox event"):
                    _review_approval(PgSessionStore(db, clock), approval_id, review, reviewer)
                assert service.action_snapshot("O1005") == before
                assert (
                    db.execute(
                        "SELECT status FROM approvals WHERE id=?", (approval_id,)
                    ).fetchone()[0]
                    == "pending"
                )
                for table in ("action_executions", "session_notifications", "outbox_events"):
                    assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
                db.execute("DROP TRIGGER suppress_outbox ON outbox_events")

            await infra.db.run(failed_outbox)
            responses = await asyncio.gather(
                *[client.post(decision, headers=supervisor, json=body) for _ in range(5)]
            )
            assert all(response.status_code == 200 for response in responses), [
                r.text for r in responses
            ]
            key = f"approval:{approval_id}"
            assert {response.json()["execution_key"] for response in responses} == {key}
            relay = OutboxRelay(infra.db, clock, payment, SandboxFulfillment())
            assert await relay.tick() == 1 and await relay.tick() == 0
            assert payment.attempts == [key]
            state = (await client.get(path, headers=buyer)).json()
            assert len(state["notifications"]) == 2
            assert state["action_executions"][0]["result"]["amount_cents"] == 25000
            assert (
                await infra.db.run(
                    lambda db: db.execute(
                        "SELECT count(*) FROM refunds WHERE execution_key=? AND status='success'",
                        (key,),
                    ).fetchone()[0]
                )
                == 1
            )
            handoff = await infra.db.run(
                lambda db: Services(db, clock).transfer_to_human("U005", "售后", "核对")
            )
            update = f"/api/admin/handoffs/{handoff}"
            assert (
                await client.patch(update, headers=supervisor, json={"status": "claimed"})
            ).status_code == 200
            assert (
                await client.patch(
                    update,
                    headers=auth("S02", "supervisor"),
                    json={"status": "closed", "resolution": "完成"},
                )
            ).status_code == 409
            closed = await client.patch(
                update, headers=supervisor, json={"status": "closed", "resolution": "完成"}
            )
            assert closed.json()["status"] == "closed"

    with_infra(check)


def test_fulfillment_precedes_refund_and_shutdown_drains_delivery() -> None:
    from aftersales.domain.gateways import SandboxFulfillment, SandboxPayment

    async def check(infra: Infra) -> None:
        clock = AdvancingClock()
        entered, release = asyncio.Event(), asyncio.Event()

        class PausedWarehouse(SandboxFulfillment):
            async def intercept(self, idempotency_key: str, order_id: str) -> None:
                entered.set()
                await release.wait()
                await super().intercept(idempotency_key, order_id)

        payment, fulfillment = SandboxPayment(clock), PausedWarehouse()
        app = create_app(
            None,
            proposal_model("cancel_order", order_id="O1006", reason="取消"),
            OPTIONS,
            clock=clock,
            production=infra.settings(),
            payment_gateway=payment,
            fulfillment_gateway=fulfillment,
        )

        async def stock() -> int:
            return await infra.db.run(
                lambda db: db.execute("SELECT stock FROM skus WHERE id='S0001'").fetchone()[0]
            )

        previous = await stock()
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        exiting = None
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://m10"
            ) as client:
                assert (await client.get("/healthz")).status_code == 200
                assert (await client.get("/readyz")).status_code == 200
                _, execution = await approve_proposal(client)
                await asyncio.wait_for(entered.wait(), timeout=4)
                assert not payment.attempts and await stock() == previous
                exiting = asyncio.create_task(lifespan.__aexit__(None, None, None))
                await asyncio.sleep(0.025)
                assert not exiting.done()
        finally:
            release.set()
            if exiting is not None:
                await asyncio.wait_for(exiting, timeout=5)
            else:
                await lifespan.__aexit__(None, None, None)
        # Cancel two reserved units of S0001 and refund full paid total of 38,000 cents.
        key = execution["execution_key"]
        assert await stock() == previous + 2
        assert payment.attempts == [key] and key in fulfillment.commands
        assert next(iter(payment.refunds.values())).amount_cents == 38000

    with_infra(check)


def test_address_outbox_preserves_per_order_sequence() -> None:
    from aftersales.core.outbox import OutboxRelay
    from aftersales.domain.gateways import SandboxFulfillment, SandboxPayment

    async def check(infra: Infra) -> None:
        clock = AdvancingClock()
        address, second = (
            {
                "receiver_name": "测试买家",
                "phone": "13800001000",
                "province": "上海市",
                "city": "上海市",
                "district": "黄浦区",
                "detail": "示例路甲号",
            },
            "示例路乙号",
        )
        models = [
            proposal_model("modify_address", order_id="O1006", address=variant)
            for variant in (address, {**address, "detail": second})
        ]
        keys = []
        for llm in models:
            async with gateway(infra, clock, llm) as client:
                _, execution = await approve_proposal(client)
                assert execution["result"]["status"] == "processing"
                keys.append(execution["execution_key"])
        fulfillment = SandboxFulfillment()
        payment = SandboxPayment(clock)
        first = OutboxRelay(infra.db, clock, payment, fulfillment)
        second_worker = OutboxRelay(infra.db, clock, payment, fulfillment)
        await asyncio.gather(first.tick(), second_worker.tick())
        while await first.tick():
            pass
        assert list(fulfillment.commands) == keys
        assert [entry[1].detail for entry in fulfillment.commands.values()] == [
            address["detail"],
            second,
        ]
        assert not payment.attempts

    with_infra(check)


def test_reconciliation_cli_repairs_and_detects_three_exact_differences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from aftersales.cli import app
    from aftersales.domain.gateways import ChannelRefund, SandboxPayment
    from aftersales.domain.settlement import apply_channel

    async def check(infra: Infra) -> None:
        clock = AdvancingClock()
        payment = SandboxPayment(clock)

        def make(
            db: PgConnection, refund_id: str, order_id: str, paid: int, terminal: bool
        ) -> ChannelRefund:
            key = f"fixture:{refund_id}"
            db.execute(
                """INSERT INTO action_executions
                   (execution_key, proposal_id, action_id, version, tool_name, canonical_args_json,
                    args_hash, result_json, before_snapshot_json, after_snapshot_json, created_at)
                   VALUES (?, 'approval', ?, 1, 'request_return', '{}', 'fixture', '{}', '{}', '{}', ?)""",
                (key, refund_id, clock.now().isoformat()),
            )
            db.execute(
                """INSERT INTO refunds (id, order_id, payment_method_id, amount_cents, status,
                       execution_key, created_at, status_changed_at)
                   SELECT ?, id, payment_method_id, ?, 'pending', ?, ?, ?
                   FROM orders WHERE id=?""",
                (refund_id, paid, key, clock.now().isoformat(), clock.now().isoformat(), order_id),
            )
            result = ChannelRefund(
                refund_id=refund_id,
                order_id=order_id,
                execution_key=key,
                amount_cents=paid,
                status="success",
            )
            apply_channel(
                db,
                clock,
                result if terminal else result.model_copy(update={"status": "processing"}),
            )
            return result

        hanging = await infra.db.run(lambda db: make(db, "R-hang", "O1001", 900, False))
        await infra.db.run(lambda db: make(db, "R-short", "O1002", 1100, True))
        mismatch = await infra.db.run(lambda db: make(db, "R-mismatch", "O1006", 5700, True))
        long = ChannelRefund(
            refund_id="R-channel-only",
            order_id="O1007",
            execution_key="external",
            amount_cents=1200,
            status="success",
        )
        for receipt in (hanging, mismatch.model_copy(update={"amount_cents": 5701}), long):
            payment.refunds[receipt.refund_id] = receipt
            payment.settled_at[receipt.refund_id] = clock.now()
        clock.advance(900)
        await infra.db.run(lambda db: make(db, "R-recent", "O1008", 1300, True))

        @asynccontextmanager
        async def channels(*args: Any) -> AsyncIterator[tuple[SandboxPayment, None]]:
            yield payment, None

        monkeypatch.setenv("DATABASE_URL", infra.url)
        monkeypatch.setenv("GATEWAY_MODE", "http")
        monkeypatch.setenv("PAYMENT_GATEWAY_URL", "http://unused.test")
        monkeypatch.setattr("aftersales.domain.gateways.open_gateways", channels)
        monkeypatch.setattr("aftersales.domain.clock.WallClock", lambda: clock)
        result = await asyncio.to_thread(CliRunner().invoke, app, ["reconcile"])
        assert result.exit_code == 0, (result.output, result.exception)
        assert json.loads(result.output) == {
            "queried": 1,
            "repaired": 1,
            "unknown": 0,
            "long_payment": 1,
            "short_payment": 1,
            "amount_mismatch": 1,
            "terminal_conflict": 0,
        }
        assert await infra.db.run(
            lambda db: [
                tuple(row.values())
                for row in db.execute(
                    """SELECT kind, refund_id, local_cents, channel_cents
               FROM reconciliation_discrepancies ORDER BY kind"""
                )
            ]
        ) == [
            ("amount_mismatch", "R-mismatch", 5700, 5701),
            ("long_payment", "R-channel-only", None, 1200),
            ("short_payment", "R-short", 1100, None),
        ]

    with_infra(check)


def test_router_opens_on_service_failure_and_recovers_through_gateway() -> None:
    from aftersales.core.llm_router import ModelRouter, ModelTarget

    async def check(infra: Infra) -> None:
        now = 0.0

        class ProviderError(RuntimeError):
            status_code = 503

        class Primary:
            healthy = False
            calls = 0

            async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
                self.calls += 1
                if not self.healthy:
                    raise ProviderError()
                return LLMResponse(text="主模型已恢复")

        primary = Primary()
        backup = AsyncMockLLM(
            [
                tool("get_order", order_id="O1006"),
                LLMResponse(text="备模型已查询"),
                LLMResponse(text="备模型服务"),
            ]
        )
        routing = ModelRouter(
            {"agent": [ModelTarget(MODEL, primary), ModelTarget("qwen-plus", backup)]},
            threshold=2,
            cooldown=2,
            monotonic=lambda: now,
        )
        async with gateway(infra, AdvancingClock(), primary, model_router=routing) as client:
            actor = auth("U006")
            created = await client.post("/api/sessions", headers=actor)
            path = f"/api/sessions/{created.json()['session_id']}/messages"
            first = await client.post(path, headers=actor, json={"content": "查订单"})
            assert first.json()["turn"]["reply"] == "备模型已查询", first.text
            assert len(first.json()["turn"]["tool_calls_made"]) == 1
            assert routing.circuits["agent", 0].state == "OPEN"
            await client.post(path, headers=actor, json={"content": "再查"})
            assert primary.calls == 2
            now, primary.healthy = 3, True
            restored = await client.post(path, headers=actor, json={"content": "看看状态"})
            assert restored.json()["turn"]["reply"] == "主模型已恢复"
            assert routing.circuits["agent", 0].state == "CLOSED"

    with_infra(check)

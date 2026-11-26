"""M9 acceptance against real PostgreSQL 16 and Redis 7."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
import pytest

from aftersales.core.config import AgentConfig
from aftersales.core.llm import AsyncMockLLM, LLMResponse, ToolCall, Usage
from aftersales.domain.clock import FixedClock
from aftersales.domain.models import OperationResult, PurchaseItem, ReturnItem
from aftersales.domain.services import PolicyError, Services
from aftersales.web.app import create_app

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from aftersales.domain.storage import PostgresStorage
    from aftersales.web.production import ProductionSettings

pytestmark = pytest.mark.integration
CLOCK = FixedClock()
PRICE_ARGS = {"order_id": "O1006", "order_item_id": "OI00006"}
MODEL = "gpt-4.1-mini"
SECRET = "m9-integration-test-key-only-000000000000"


@dataclass
class Infra:
    db: PostgresStorage
    redis: Redis
    url: str
    redis_url: str
    namespace: str

    def settings(self, *, budget: int = 10_000_000) -> ProductionSettings:
        from aftersales.web.production import ProductionSettings
        from aftersales.web.security import JwtSettings

        return ProductionSettings(
            self.url,
            self.redis_url,
            JwtSettings("HS256", SECRET, "m9-tests", "gateway"),
            daily_budget=budget,
            redis_namespace=self.namespace,
        )


def with_infra(
    test: Callable[[Infra], Awaitable[None]],
    *,
    seed: bool = True,
    migration_target: int = 2,
) -> None:
    from psycopg.conninfo import make_conninfo
    from redis.asyncio import Redis

    from aftersales.domain.migrate import migrate
    from aftersales.domain.storage import PostgresStorage

    async def run() -> None:
        url = os.environ.get(
            "M9_TEST_DATABASE_URL",
            "postgresql://aftersales:aftersales_dev@127.0.0.1:5432/aftersales",
        )
        redis_url = os.environ.get("M9_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
        schema = "m9_" + uuid4().hex
        isolated = make_conninfo(url, options=f"-c search_path={schema}")
        admin, db = PostgresStorage(url), PostgresStorage(isolated, max_size=40)
        async with admin.pool, Redis.from_url(redis_url) as redis:
            await admin.pool.wait(timeout=10)
            assert (await redis.info("server"))["redis_version"].split(".")[0] == "7"
            await admin.run(lambda c: c.execute(f"CREATE SCHEMA {schema}"))
            try:
                async with db.pool:
                    await db.run(
                        lambda c: migrate(c, seed=seed, clock=CLOCK, target=migration_target)
                    )
                    await test(Infra(db, redis, isolated, redis_url, schema))
            finally:
                await admin.run(lambda c: c.execute(f"DROP SCHEMA {schema} CASCADE"))
                keys = [key async for key in redis.scan_iter(f"{schema}:*")]
                if keys:
                    await redis.delete(*keys)

    loop = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop) as runner:
        runner.run(run())


async def race(
    infra: Infra, count: int, attempt: Callable[[Services, int], OperationResult]
) -> tuple[list[OperationResult], Counter[str]]:
    # Prewarm every connection so pool growth cannot serialize the contending transactions.
    async with AsyncExitStack() as connections:
        for _ in range(count):
            await connections.enter_async_context(infra.db.pool.connection())
    barrier = asyncio.Barrier(count)

    async def call(index: int) -> OperationResult | PolicyError:
        await barrier.wait()
        try:
            return await infra.db.run(lambda db: attempt(Services(db, CLOCK), index))
        except PolicyError as exc:
            return exc

    async with asyncio.timeout(45):
        values = await asyncio.gather(*(call(i) for i in range(count)))
    return (
        [v for v in values if isinstance(v, OperationResult)],
        Counter(rule for v in values if isinstance(v, PolicyError) for rule in v.rule_ids),
    )


def test_inventory_and_crossed_sku_lock_order() -> None:
    async def check(infra: Infra) -> None:
        await infra.db.run(lambda db: db.execute("UPDATE skus SET stock=1 WHERE id='S0004'"))
        winners, failures = await race(
            infra,
            20,
            lambda svc, i: svc.create_order(
                f"U{i + 1:03}",
                [PurchaseItem(sku_id="S0004", qty=1)],
                f"PM{i + 1:04}",
            ),
        )
        assert len(winners) == 1 and failures == {"STOCK": 19}
        assert winners[0].order_id == "O1301"
        assert (
            await infra.db.run(
                lambda db: db.execute("SELECT stock FROM skus WHERE id='S0004'").fetchone()[0]
            )
            == 0
        )
        await infra.db.run(
            lambda db: db.execute("UPDATE skus SET stock=40 WHERE id IN ('S0001','S0002')")
        )

        def crossed(svc: Services, i: int) -> OperationResult:
            skus = ("S0001", "S0002") if i % 2 else ("S0002", "S0001")
            return svc.create_order(
                f"U{i + 1:03}",
                [PurchaseItem(sku_id=sku, qty=1) for sku in skus],
                f"PM{i + 1:04}",
            )

        winners, failures = await race(infra, 40, crossed)
        assert len(winners) == len({item.order_id for item in winners}) == 40
        assert not failures
        assert await infra.db.run(
            lambda db: [
                row[0]
                for row in db.execute(
                    "SELECT stock FROM skus WHERE id IN ('S0001','S0002') ORDER BY id"
                )
            ]
        ) == [0, 0]
        print("stock: 1 success / 19 STOCK; crossed SKU: 40 successes / 0 deadlocks")

    with_infra(check)


def test_coupon_price_protection_and_return_contention() -> None:
    async def check(infra: Infra) -> None:
        # Each cart costs 25,000 cents. One 2,000-cent coupon makes one 23,000-cent order.
        await infra.db.run(
            lambda db: db.execute(
                "UPDATE skus SET stock=5, price_cents=25000 WHERE id BETWEEN 'S0021' AND 'S0030'"
            )
        )
        winners, failures = await race(
            infra,
            10,
            lambda svc, i: svc.create_order(
                "U001",
                [PurchaseItem(sku_id=f"S{i + 21:04}", qty=1)],
                "PM0001",
                "UC0001",
            ),
        )
        assert len(winners) == 1 and failures == {"P13": 9}
        assert winners[0].amount_cents == 23000
        assert (
            await infra.db.run(
                lambda db: db.execute(
                    "SELECT used_order_id FROM user_coupons WHERE id='UC0001'"
                ).fetchone()[0]
            )
            == winners[0].order_id
        )
        # Two units dropped by 3,000 cents each; 24,700 / 26,000 paid share gives 5,700 cents.
        prices, failures = await race(
            infra, 10, lambda svc, _: svc.apply_price_protection(**PRICE_ARGS)
        )
        assert len(prices) == 1 and failures == {"P04": 9}
        assert prices[0].amount_cents == 5700
        # A full return of the only 13,000-cent line leaves zero refundable balance.
        returns, failures = await race(
            infra,
            10,
            lambda svc, _: svc.request_return(
                "O1002", [ReturnItem(order_item_id="OI00002", qty=1)], "并发退货"
            ),
        )
        assert len(returns) == 1 and failures == {"P06": 9}
        assert returns[0].amount_cents == 13000
        ledger = await infra.db.run(
            lambda db: [
                (r[0], r[1], r[2], r[3])
                for r in db.execute(
                    """SELECT o.id, o.paid_cents, sum(r.amount_cents), count(r.id)
                   FROM orders o JOIN refunds r ON r.order_id=o.id
                   WHERE o.id IN ('O1002', 'O1006') GROUP BY o.id ORDER BY o.id"""
                )
            ]
        )
        assert ledger == [("O1002", 13000, 13000, 1), ("O1006", 38000, 5700, 1)]
        print("coupon/price protection/return: each 1 success / 9 rejected")

    with_infra(check)


def test_database_refund_constraints_and_migration_reentry() -> None:
    from psycopg.errors import CheckViolation, ForeignKeyViolation

    from aftersales.domain.migrate import migrate

    async def check(infra: Infra) -> None:
        assert (
            await infra.db.run(lambda db: int(db.execute("SHOW server_version_num").fetchone()[0]))
            // 10000
            == 16
        )
        await infra.db.run(lambda db: migrate(db, seed=True, clock=CLOCK))
        assert (
            await infra.db.run(lambda db: db.execute("SELECT count(*) FROM orders").fetchone()[0])
            == 300
        )
        sql = """INSERT INTO refunds (id, order_id, amount_cents, payment_method_id, status, created_at)
                 VALUES ('R99999','O1002',?,?,'success',?)"""
        stamp = CLOCK.now().isoformat()
        # The 13,000-cent order belongs to PM0002. Both failed statements roll back the ledger.
        with pytest.raises(ForeignKeyViolation):
            await infra.db.run(lambda db: db.execute(sql, (1, "PM0001", stamp)))
        with pytest.raises(CheckViolation):
            await infra.db.run(lambda db: db.execute(sql, (13001, "PM0002", stamp)))
        await infra.db.run(lambda db: db.execute(sql, (13000, "PM0002", stamp)))
        assert (
            await infra.db.run(
                lambda db: db.execute(
                    "SELECT amount_cents FROM order_refund_totals WHERE order_id='O1002'"
                ).fetchone()[0]
            )
            == 13000
        )

    with_infra(check)


def auth(user: str, role: str = "buyer", **claims: Any) -> dict[str, str]:
    import jwt

    token = jwt.encode(
        {
            "sub": user,
            "role": role,
            "iss": "m9-tests",
            "aud": "gateway",
            "exp": int(time.time()) + 600,
            **claims,
        },
        SECRET,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def tool(name: str, /, **arguments: Any) -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id=name, name=name, arguments=arguments)],
        stop_reason="tool_use",
        usage=Usage(input_tokens=10, output_tokens=5),
    )


def test_cross_instance_proposal_recovery_and_tenant_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aftersales.domain.storage import PostgresStorage

    class CountedStorage(PostgresStorage):
        active = 0

        async def run(self, work: Callable[..., Any]) -> Any:
            CountedStorage.active += 1
            try:
                return await super().run(work)
            finally:
                CountedStorage.active -= 1

    class AuditedMock(AsyncMockLLM):
        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            assert CountedStorage.active == 0
            await asyncio.sleep(0.01)
            assert CountedStorage.active == 0
            return await super().chat(*args, **kwargs)

    monkeypatch.setattr("aftersales.web.production.PostgresStorage", CountedStorage)

    async def check(infra: Infra) -> None:
        options = AgentConfig(proposal_gate=True, model=MODEL)
        buyer, other = auth("U006"), auth("U001")
        first_llm = AuditedMock(
            [
                tool(
                    "propose_actions",
                    actions=[{"tool_name": "apply_price_protection", "arguments": PRICE_ARGS}],
                ),
                LLMResponse(text="O1006 可原路退差 57.00 元，请确认。"),
            ]
        )
        second_llm = AuditedMock(
            [
                tool("apply_price_protection", **PRICE_ARGS),
                LLMResponse(text="已原路退差 57.00 元。"),
                tool("find_user_by_phone", phone="13800000001", name="用户001"),
                tool("get_order", order_id="O1001"),
                LLMResponse(text="仅可处理当前账号。"),
            ]
        )

        def application(llm: AuditedMock) -> Any:
            return create_app(
                None,
                llm,
                options,
                production=infra.settings(),
                clock=CLOCK,
                trace_dir=tmp_path / "unused",
            )

        first = application(first_llm)
        async with (
            first.router.lifespan_context(first),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=first), base_url="http://one"
            ) as client,
        ):
            assert (await client.post("/api/sessions")).status_code == 401
            assert (
                await client.post("/api/sessions", headers=auth("U006", aud="other"))
            ).status_code == 401
            assert (
                await client.post("/api/sessions", headers=auth("U006", "admin"))
            ).status_code == 403
            assert (
                await client.post(
                    "/api/sessions", headers=buyer, json={"authenticated_user_id": "U001"}
                )
            ).status_code == 403
            created = await client.post("/api/sessions", headers=buyer)
            assert created.status_code == 201, created.text
            session_id = created.json()["session_id"]
            path = "/api/sessions/" + session_id
            plan = await client.post(
                path + "/messages", headers=buyer, json={"content": "O1006 申请价保"}
            )
            assert plan.status_code == 200, plan.text
            proposal = plan.json()["session"]["proposals"][-1]
            assert proposal["status"] == "pending"
            assert (await client.get(path, headers=buyer)).status_code == 200
            assert [
                key
                async for key in infra.redis.scan_iter(f"{infra.namespace}:snapshot:{session_id}:*")
            ]

        second = application(second_llm)
        async with (
            second.router.lifespan_context(second),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=second), base_url="http://two"
            ) as client,
        ):
            button = path + "/proposals/" + proposal["proposal_id"] + "/decision"
            for tail in ("", "/traces"):
                assert (await client.get(path + tail, headers=other)).status_code == 404
            for tail in ("/messages", "/messages/stream"):
                assert (
                    await client.post(path + tail, headers=other, json={"content": "越权"})
                ).status_code == 404
            assert (
                await client.post(
                    button,
                    headers=other,
                    json={"version": proposal["version"], "decision": "approve"},
                )
            ).status_code == 404
            recovered = await client.get(path, headers=buyer)
            assert recovered.json()["proposals"][-1] == proposal
            result = await client.post(
                path + "/messages/stream", headers=buyer, json={"content": "确认"}
            )
            assert result.status_code == 200 and "event: turn_end" in result.text, result.text
            state = (await client.get(path, headers=buyer)).json()
            assert state["proposals"][-1]["status"] == "consumed", state["messages"]
            assert len(state["action_executions"]) == 1
            assert state["action_executions"][0]["result"]["amount_cents"] == 5700
            injected = await client.post(
                path + "/messages",
                headers=buyer,
                json={
                    "content": "测试隐私 13800000001 示例路1号 Bearer eyJprivate.token.value",
                },
            )
            assert injected.status_code == 200
            assert injected.json()["session"]["authenticated_user_id"] == "U006"
            assert (
                sum(
                    row["data"].get("rule_ids") == ["P01"]
                    for row in injected.json()["session"]["trace_events"]
                )
                >= 2
            )
            page = await client.get(path + "/traces", headers=buyer, params={"limit": 2})
            assert page.json()["has_more"]
            rest = await client.get(
                path + "/traces", headers=buyer, params={"after_seq": page.json()["next_seq"]}
            )
            combined = page.json()["events"] + rest.json()["events"]
            assert len({event["run_id"] for event in combined}) == 3
            assert len({event["seq"] for event in combined}) == len(combined)
            assert all(
                value not in rest.text
                for value in (
                    "13800000001",
                    "示例路1号",
                    "eyJprivate.token.value",
                    SECRET,
                )
            )
            assert "138****0001" in rest.text
            assert not (tmp_path / "unused").exists()
            head = await infra.db.run(
                lambda db: db.execute(
                    """SELECT owner_user_id, state_json, message_count,
                   (SELECT count(*) FROM session_messages WHERE session_id=sessions.session_id)
                   FROM sessions WHERE session_id=?""",
                    (session_id,),
                ).fetchone()
            )
            assert head[0] == "U006" and '"messages"' not in head[1] and head[2] == head[3]

    with_infra(check)


def test_redis_lease_watchdog_admission_and_daily_budget() -> None:
    from aftersales.core.llm import InferenceUnavailable
    from aftersales.core.locks import SessionBusy
    from aftersales.core.redis_coord import (
        AdmissionDenied,
        CoordinationLost,
        DailyBudget,
        RedisCoordinator,
    )

    async def check(infra: Infra) -> None:
        a = RedisCoordinator(
            infra.redis,
            namespace=infra.namespace,
            lease_ms=300,
            renew_seconds=0.05,
            request_limit=3,
            window_ms=650,
        )
        b = RedisCoordinator(
            infra.redis,
            namespace=infra.namespace,
            lease_ms=300,
            renew_seconds=0.05,
            request_limit=3,
            window_ms=650,
        )
        async with a.acquire("session"):
            await asyncio.sleep(0.45)
            with pytest.raises(SessionBusy):
                async with b.acquire("session"):
                    pytest.fail("Lease must be renewed")
        key = a.key("lock:session:session")
        assert not await infra.redis.exists(key)
        with pytest.raises(CoordinationLost):
            async with a.acquire("session"):
                await infra.redis.set(key, "new-owner", px=1000)
                await a.require_current_lease("session")
        assert await infra.redis.get(key) == b"new-owner"
        with pytest.raises(CoordinationLost):
            async with a.acquire("other-session"):
                await infra.redis.delete(a.key("lock:session:other-session"))
                await asyncio.sleep(0.4)
        async with a.admit("U001"), b.admit("U001"):
            with pytest.raises(AdmissionDenied, match="两条"):
                async with b.admit("U001"):
                    pytest.fail("Third request must fail")
            await asyncio.sleep(0.4)
            with pytest.raises(AdmissionDenied, match="两条"):
                async with b.admit("U001"):
                    pytest.fail("Active slots must survive the base TTL")
        async with a.admit("U001"):
            pass
        with pytest.raises(AdmissionDenied, match="频繁"):
            async with b.admit("U001"):
                pytest.fail("The fourth admitted request must fail")
        await asyncio.sleep(0.3)
        async with b.admit("U001"):
            pass
        budget = DailyBudget(a, CLOCK, "U001", 1000)
        await budget.before(MODEL)
        # 0.000001 USD per completed call equals 100 microcents. Ten calls reach 1,000.
        await asyncio.gather(*(budget.record(Usage(), Decimal("0.000001")) for _ in range(10)))
        assert await infra.redis.get(budget.key()) == b"1000"
        assert 172790 <= await infra.redis.ttl(budget.key()) <= 172800
        with pytest.raises(InferenceUnavailable, match="人工客服"):
            await budget.before(MODEL)
        await DailyBudget(b, CLOCK, "U002", 1000).before(MODEL)

    with_infra(check, seed=False)


def test_rs256_and_gateway_budget_deny_new_model_calls() -> None:
    from dataclasses import replace

    import jwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from aftersales.web.security import JwtSettings

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = (
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode("ascii")
    )

    async def check(infra: Infra) -> None:
        settings = replace(
            infra.settings(budget=1), jwt=JwtSettings("RS256", public, "m9-tests", "gateway")
        )
        mock = AsyncMockLLM([tool("lookup_policy", topic="auth")])
        app = create_app(
            None,
            mock,
            AgentConfig(proposal_gate=True, model=MODEL),
            production=settings,
            clock=CLOCK,
        )
        token = jwt.encode(
            {
                "sub": "U001",
                "role": "buyer",
                "iss": "m9-tests",
                "aud": "gateway",
                "exp": int(time.time()) + 600,
            },
            private,
            algorithm="RS256",
        )
        buyer = {"Authorization": f"Bearer {token}"}
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://m9"
            ) as client,
        ):
            assert (await client.post("/api/sessions", headers=auth("U001"))).status_code == 401
            created = await client.post("/api/sessions", headers=buyer)
            assert created.status_code == 201, created.text
            path = f"/api/sessions/{created.json()['session_id']}/messages"
            first = await client.post(path, headers=buyer, json={"content": "查询规则"})
            assert first.status_code == 200, first.text
            assert "人工客服" in first.json()["turn"]["reply"]
            # 10 input tokens at $0.40/M and 5 output at $1.60/M = $0.000012 = 1,200 microcents.
            assert await infra.redis.get(f"{infra.namespace}:budget:uid:U001:2026-11-15") == b"1200"
            retry = await client.post(path, headers=buyer, json={"content": "再查询"})
            assert retry.status_code == 429
            assert len(mock.requests) == 1

    with_infra(check)


def test_cross_instance_lease_and_otlp_export() -> None:
    from dataclasses import replace

    from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
        ExportMetricsServiceRequest,
    )
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    async def check(infra: Infra) -> None:
        entered, proceed = asyncio.Event(), asyncio.Event()
        captured: dict[str, bytes] = {}

        async def receive(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request_line, *headers = (await reader.readuntil(b"\r\n\r\n")).split(b"\r\n")
                _, path, _ = request_line.decode().split()
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in headers
                    if line.lower().startswith(b"content-length:")
                )
                captured[path] = await reader.readexactly(length)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        class PausedModel:
            calls = 0

            async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
                entered.set()
                await proceed.wait()
                self.calls += 1
                if self.calls == 1:
                    return LLMResponse(
                        tool_calls=[
                            ToolCall(
                                id="foreign", name="get_order", arguments={"order_id": "O1001"}
                            ),
                            ToolCall(
                                id="unconfirmed",
                                name="apply_price_protection",
                                arguments=PRICE_ARGS,
                            ),
                        ],
                        stop_reason="tool_use",
                    )
                return LLMResponse(text="处理结束", usage=Usage(input_tokens=12, output_tokens=8))

        async with await asyncio.start_server(receive, "127.0.0.1", 0) as receiver:
            endpoint = f"http://127.0.0.1:{receiver.sockets[0].getsockname()[1]}"
            options = AgentConfig(proposal_gate=True, model=MODEL)
            first = create_app(
                None,
                PausedModel(),
                options,
                clock=CLOCK,
                production=replace(infra.settings(), otlp_endpoint=endpoint),
            )
            second = create_app(
                None, AsyncMockLLM([]), options, clock=CLOCK, production=infra.settings()
            )
            async with (
                first.router.lifespan_context(first),
                second.router.lifespan_context(second),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=first),
                    base_url="http://one",
                    headers=auth("U006"),
                ) as one,
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=second),
                    base_url="http://two",
                    headers=auth("U006"),
                ) as two,
            ):
                created = await one.post("/api/sessions")
                assert created.status_code == 201
                path = f"/api/sessions/{created.json()['session_id']}/messages"
                pending = asyncio.create_task(one.post(path, json={"content": "等待模型"}))
                try:
                    await asyncio.wait_for(entered.wait(), timeout=3)
                    response = await two.post(path, json={"content": "同时发送"})
                    assert response.status_code == 409, response.text
                finally:
                    proceed.set()
                    response = await pending
                assert response.status_code == 200

        traces = ExportTraceServiceRequest.FromString(captured["/v1/traces"])
        names = {
            span.name
            for resource in traces.resource_spans
            for scope in resource.scope_spans
            for span in scope.spans
        }
        assert names == {"aftersales.session.turn", "aftersales.llm.call"}
        metrics = ExportMetricsServiceRequest.FromString(captured["/v1/metrics"])
        names = {
            metric.name
            for resource in metrics.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        assert {
            "aftersales.http.requests",
            "aftersales.llm.tokens",
            "aftersales.llm.duration",
            "aftersales.policy.denials",
        } <= names

    with_infra(check)

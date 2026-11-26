"""Production component assembly and request-bound ownership checks."""

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TypeVar

from dotenv import dotenv_values
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aftersales.core.loop import Session
from aftersales.core.outbox import OutboxRelay
from aftersales.core.pg_sessions import PgSessionStore
from aftersales.core.redis_coord import DailyBudget, RedisCoordinator
from aftersales.core.telemetry import TRACE_RUN_STRIDE, PgTraceSink, Telemetry
from aftersales.core.tracing import TraceEvent
from aftersales.domain.clock import Clock
from aftersales.domain.gateways import (
    FulfillmentGateway,
    GatewaySettings,
    PaymentGateway,
    open_gateways,
)
from aftersales.domain.storage import PostgresStorage
from aftersales.web.security import JwtAuthenticator, JwtSettings, Principal
from aftersales.web.webhooks import PAYMENT_WEBHOOK_PATH

T = TypeVar("T")
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProductionSettings:
    database_url: str = field(repr=False)
    redis_url: str = field(repr=False)
    jwt: JwtSettings
    daily_budget: int = 10_000_000
    redis_namespace: str = "aftersales"
    otlp_endpoint: str | None = field(default=None, repr=False)
    model_rates: tuple[Decimal, Decimal] | None = None
    gateways: GatewaySettings = field(default_factory=GatewaySettings)
    webhook_secret: bytes | None = field(default=None, repr=False)
    outbox_max_age: int = 300

    def __post_init__(self) -> None:
        if self.daily_budget <= 0 or not self.database_url or not self.redis_url:
            raise ValueError("需要数据库、Redis 与正数每日模型额度")
        if self.model_rates and any(not rate.is_finite() or rate <= 0 for rate in self.model_rates):
            raise ValueError("自定义模型费率必须为正数")
        if self.webhook_secret is not None and len(self.webhook_secret) < 32:
            raise ValueError("支付回调密钥至少需要 32 字节")
        if self.outbox_max_age < 1:
            raise ValueError("Outbox 积压检查秒数必须为正数")

    @classmethod
    def from_env(cls) -> "ProductionSettings":
        values = {**dotenv_values(".env"), **os.environ}
        key = values.get("JWT_KEY") or ""
        algorithm = values.get("JWT_ALGORITHM") or "HS256"
        if algorithm not in {"HS256", "RS256"}:
            raise ValueError("JWT_ALGORITHM 只支持 HS256 或 RS256")
        inp, out = values.get("MODEL_INPUT_USD_PER_MTOK"), values.get("MODEL_OUTPUT_USD_PER_MTOK")
        webhook_secret = values.get("PAYMENT_WEBHOOK_SECRET") or None
        if bool(inp) != bool(out):
            raise ValueError("请同时配置模型输入、输出费率")
        return cls(
            values.get("DATABASE_URL") or "",
            values.get("REDIS_URL") or "",
            JwtSettings(
                algorithm, key, values.get("JWT_ISSUER") or "", values.get("JWT_AUDIENCE") or ""
            ),
            daily_budget=int(values.get("DAILY_BUDGET_MICROCENTS") or "10000000"),
            redis_namespace=values.get("REDIS_NAMESPACE") or "aftersales",
            otlp_endpoint=values.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            model_rates=(Decimal(inp), Decimal(out)) if inp and out else None,
            gateways=GatewaySettings.from_values(values),
            webhook_secret=webhook_secret.encode() if webhook_secret else None,
            outbox_max_age=int(values.get("OUTBOX_READY_MAX_AGE_SECONDS") or 300),
        )


class Production:
    def __init__(
        self,
        settings: ProductionSettings,
        clock: Clock,
        *,
        payment: PaymentGateway | None = None,
        fulfillment: FulfillmentGateway | None = None,
    ) -> None:
        self.settings, self.clock = settings, clock
        self.auth = JwtAuthenticator(settings.jwt)
        self.storage = PostgresStorage(settings.database_url)
        self.redis = Redis.from_url(settings.redis_url, socket_timeout=3, socket_connect_timeout=3)
        self.coordinator = RedisCoordinator(self.redis, namespace=settings.redis_namespace)
        self.telemetry = Telemetry(settings.otlp_endpoint)
        self.payment, self.fulfillment = payment, fulfillment
        self.relay: asyncio.Task[None] | None = None
        self.accepting = False

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        try:
            async with (
                self.storage.pool,
                open_gateways(self.settings.gateways, self.clock) as channels,
            ):
                await self.storage.pool.wait(timeout=10)
                await self.redis.ping()
                await self.run(
                    lambda store: store.db.execute("SELECT id FROM outbox_events LIMIT 0")
                )
                payment, fulfillment = self.payment or channels[0], self.fulfillment or channels[1]
                stopping = asyncio.Event()
                if payment is not None and fulfillment is not None:
                    relay = OutboxRelay(self.storage, self.clock, payment, fulfillment)
                    self.relay = asyncio.create_task(relay.run(stopping))
                self.accepting = True
                try:
                    yield
                finally:
                    self.accepting = False
                    stopping.set()
                    if self.relay is not None:
                        await self.relay
                        self.relay = None
        finally:
            await self.redis.aclose()
            await self.telemetry.close()

    async def run(self, work: Callable[[PgSessionStore], T]) -> T:
        return await self.storage.run(lambda db: work(PgSessionStore(db, self.clock)))

    async def readiness(self) -> tuple[bool, dict[str, int | bool]]:
        async def postgres() -> tuple[int, int]:
            row = await self.run(
                lambda store: store.db.execute(
                    """SELECT count(*) FILTER (WHERE status='dead_letter'),
                   coalesce(extract(epoch FROM (?::timestamptz -
                       min(created_at) FILTER (WHERE status IN ('pending','processing')))), 0)::bigint
                   FROM outbox_events""",
                    (self.clock.now().isoformat(),),
                ).fetchone()
            )
            return row[0], max(row[1], 0)

        async with asyncio.timeout(3):
            pg, redis = await asyncio.gather(postgres(), self.redis.ping(), return_exceptions=True)
        pg_ok, redis_ok = isinstance(pg, tuple), redis is True
        dead, age = pg if isinstance(pg, tuple) else (0, 0)
        relay = self.relay is not None and not self.relay.done()
        checks = {
            "pg": pg_ok,
            "redis": redis_ok,
            "relay": relay,
            "outbox_dead_letters": dead,
            "outbox_oldest_seconds": age,
        }
        return (
            self.accepting
            and pg_ok
            and redis_ok
            and relay
            and dead == 0
            and age <= self.settings.outbox_max_age
        ), checks

    def budget(self, user_id: str) -> DailyBudget:
        return DailyBudget(
            self.coordinator,
            self.clock,
            user_id,
            self.settings.daily_budget,
            self.settings.model_rates,
        )

    def writer(self, session_id: str) -> PgTraceSink:
        return PgTraceSink(self.clock, self.storage, self.telemetry, session_id)

    async def snapshot(self, session_id: str, principal: Principal) -> Session:
        revision = await self.run(lambda store: store.require_owner(session_id, principal.user_id))
        key = self.coordinator.key(f"snapshot:{session_id}:{revision}")
        try:
            if cached := await self.redis.get(key):
                session = Session.model_validate_json(cached)
                if (
                    session.authenticated_user_id == principal.user_id
                    and session.session_id == session_id
                ):
                    session._owner_user_id = principal.user_id
                    session._revision = revision
                    session._persisted_messages = len(session.messages)
                    return session
        except (RedisError, ValidationError):
            pass
        session = await self.run(lambda store: store.load(session_id, attach=False))
        try:
            actual_key = self.coordinator.key(f"snapshot:{session_id}:{session._revision}")
            await self.redis.set(actual_key, session.model_dump_json(), ex=120, nx=True)
        except RedisError:
            pass
        return session

    @staticmethod
    def audit_events(
        store: PgSessionStore,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int = 200,
        newest: bool = False,
        run_number: int | None = None,
    ) -> list[tuple[int, str, TraceEvent]]:
        first, after = divmod(after_seq, TRACE_RUN_STRIDE)
        first += 1
        direction = "DESC" if newest else "ASC"
        run_filter = "AND a.run_number=?" if run_number is not None else ""
        params = (run_number,) if run_number is not None else ()
        rows = store.db.execute(
            f"""SELECT a.run_number, r.run_id, a.seq, a.timestamp, a.event, a.data
             FROM audit_traces a JOIN session_traces r USING (session_id, run_number)
             WHERE a.session_id=? AND (a.run_number, a.seq) > (?, ?) {run_filter}
             ORDER BY a.run_number {direction}, a.seq {direction} LIMIT ?""",
            (session_id, first, after, *params, limit),
        )
        result = [
            (
                row["run_number"],
                row["run_id"],
                TraceEvent(
                    seq=row["seq"],
                    timestamp=row["timestamp"],
                    event=row["event"],
                    data=json.loads(row["data"]),
                ),
            )
            for row in rows
        ]
        return list(reversed(result)) if newest else result


class ProductionMiddleware:
    def __init__(self, app: ASGIApp, production: Production) -> None:
        self.app, self.production = app, production

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        self.production.telemetry.requests.add(1, {"method": scope["method"]})
        started = False

        async def tracked(message: Message) -> None:
            nonlocal started
            started |= message["type"] == "http.response.start"
            await send(message)

        try:
            payment_callback = scope["path"] == PAYMENT_WEBHOOK_PATH and scope["method"] == "POST"
            if scope["path"].startswith("/api/") and not payment_callback:
                authorization = Headers(scope=scope).get("authorization", "")
                principal = self.production.auth.authenticate(authorization)
                if scope["path"].startswith("/api/admin/"):
                    principal.require("supervisor", "admin")
                scope.setdefault("state", {})["principal"] = principal
            await self.app(scope, receive, tracked)
        except HTTPException as exc:
            if not started:
                await JSONResponse(
                    {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
                )(scope, receive, send)
        except Exception as exc:  # noqa: BLE001 - prevent sensitive driver error details reaching logs
            LOGGER.error("Production request failed (%s)", type(exc).__name__)
            if not started:
                await JSONResponse({"detail": "服务暂不可用，请稍后重试"}, status_code=503)(
                    scope, receive, send
                )
            else:
                await send({"type": "http.response.body", "body": b"", "more_body": False})

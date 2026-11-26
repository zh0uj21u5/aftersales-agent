"""Run an isolated M10 mixed workload on real PostgreSQL and Redis."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import secrets
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import jwt
from psycopg.conninfo import make_conninfo
from redis.asyncio import Redis

from aftersales.core.config import AgentConfig
from aftersales.core.llm import LLMResponse
from aftersales.domain.clock import WallClock
from aftersales.domain.gateways import SandboxFulfillment, SandboxPayment
from aftersales.domain.migrate import migrate
from aftersales.domain.models import ApprovalRequest, PurchaseItem
from aftersales.domain.services import PolicyError, Services
from aftersales.domain.storage import PostgresStorage
from aftersales.web.app import create_app
from aftersales.web.production import ProductionSettings
from aftersales.web.security import JwtSettings


class LoadModel:
    async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
        return LLMResponse(text="已收到。")


async def run_workload(
    storage: PostgresStorage,
    settings: ProductionSettings,
    clock: WallClock,
    concurrency: int,
    repetitions: int,
    fault: bool,
) -> dict[str, Any]:
    payment = SandboxPayment(clock, ("timeout", "lost_response") if fault else ())
    app = create_app(
        None,
        LoadModel(),
        AgentConfig(proposal_gate=True, model="gpt-4.1-mini"),
        clock=clock,
        production=settings,
        payment_gateway=payment,
        fulfillment_gateway=SandboxFulfillment(),
    )
    semaphore = asyncio.Semaphore(concurrency)
    timings: list[float] = []

    def headers(user_id: str, role: str = "buyer") -> dict[str, str]:
        token = jwt.encode(
            {
                "sub": user_id,
                "role": role,
                "iss": "load-test",
                "aud": "m10",
                "exp": int(time.time()) + 300,
            },
            settings.jwt.key,
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}

    async def measured(fn: Callable[[], Awaitable[str]]) -> str:
        async with semaphore:
            start = time.perf_counter()
            result = await fn()
            timings.append((time.perf_counter() - start) * 1000)
            return result

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://load-test"
        ) as http,
    ):
        owner = await http.post("/api/sessions", headers=headers("U005"))
        owner.raise_for_status()
        approval = await storage.run(
            lambda db: Services(db, clock).submit_approval(
                "O1005",
                ApprovalRequest(
                    action="request_return",
                    args={
                        "order_id": "O1005",
                        "items": [{"order_item_id": "OI00005", "qty": 3}],
                        "reason": "压测",
                    },
                ),
                "压测 P08",
                session_id=owner.json()["session_id"],
                requested_amount_cents=45000,
            )
        )
        approval_id = approval.record_ids[0]
        await storage.run(lambda db: db.execute("UPDATE skus SET stock=1 WHERE id='S0004'"))

        async def buy() -> str:
            try:
                await storage.run(
                    lambda db: Services(db, clock).create_order(
                        "U001",
                        [PurchaseItem(sku_id="S0004", qty=1)],
                        "PM0001",
                    )
                )
            except PolicyError as exc:
                if exc.rule_ids == ["STOCK"]:
                    return "stock_rejected"
                raise
            return "order_created"

        async def approve() -> str:
            response = await http.post(
                f"/api/admin/approvals/{approval_id}/decision",
                headers=headers("S01", "supervisor"),
                json={"decision": "approve", "review_note": "压测"},
            )
            response.raise_for_status()
            if response.json()["execution_key"] != f"approval:{approval_id}":
                raise RuntimeError("Approval replay returned a different execution key")
            return "approval_ok"

        async def chat(index: int) -> str:
            buyer = headers(f"U{11 + index % 40:03}")
            session = await http.post("/api/sessions", headers=buyer)
            session.raise_for_status()
            path = "/api/sessions/" + session.json()["session_id"]
            response = await http.post(path + "/messages", headers=buyer, json={"content": "查询"})
            response.raise_for_status()
            if response.json()["turn"]["reply"] != "已收到。":
                raise RuntimeError("Unexpected turn result")
            response = await http.get(path, headers=buyer)
            response.raise_for_status()
            return "session_ok"

        start = time.perf_counter()
        outcomes = await asyncio.gather(
            *(measured(buy) for _ in range(20)),
            *(measured(approve) for _ in range(repetitions)),
            *(measured(lambda index=index: chat(index)) for index in range(repetitions)),
        )
        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        async with asyncio.timeout(20):
            while not await storage.run(
                lambda db: db.execute(
                    "SELECT 1 FROM outbox_events WHERE execution_key=? AND status='done'",
                    (f"approval:{approval_id}",),
                ).fetchone()
            ):
                await asyncio.sleep(0.05)
        status = await storage.run(
            lambda db: tuple(
                db.execute(
                    """SELECT count(*), sum(amount_cents)::bigint, min(status)
                       FROM refunds WHERE execution_key=?""",
                    (f"approval:{approval_id}",),
                )
                .fetchone()
                .values()
            )
        )

        dead_letters = await storage.run(
            lambda db: db.execute(
                "SELECT count(*) FROM outbox_events WHERE status='dead_letter'"
            ).fetchone()[0]
        )

    ordered = sorted(timings)
    counts = {name: outcomes.count(name) for name in sorted(set(outcomes))}
    expected_attempts = 3 if fault else 1
    if (
        counts
        != {
            "order_created": 1,
            "stock_rejected": 19,
            "approval_ok": repetitions,
            "session_ok": repetitions,
        }
        or status != (1, 45000, "success")
        or len(payment.attempts) != expected_attempts
        or len(set(payment.attempts)) != 1
        or len(payment.refunds) != 1
        or dead_letters != 0
    ):
        raise RuntimeError("Mixed workload invariants failed")
    return {
        "concurrency": concurrency,
        "operations": len(outcomes),
        "counts": counts,
        "elapsed_ms": elapsed_ms,
        "p50_ms": round(ordered[math.ceil(len(ordered) * 0.50) - 1], 2),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 2),
        "channel_attempts": len(payment.attempts),
        "unique_channel_keys": len(set(payment.attempts)),
        "refunds": status[0],
        "refund_cents": status[1],
        "dead_letters": dead_letters,
    }


async def run(
    database: str, redis_url: str, concurrency: int, repetitions: int, fault: bool
) -> dict[str, Any]:
    schema = "m10_load_" + uuid4().hex
    clock = WallClock()
    url = make_conninfo(database, options=f"-c search_path={schema}")
    storage, admin = PostgresStorage(url, max_size=40), PostgresStorage(database)
    settings = ProductionSettings(
        url,
        redis_url,
        JwtSettings("HS256", secrets.token_urlsafe(32), "load-test", "m10"),
        redis_namespace=schema,
    )
    async with admin.pool, Redis.from_url(redis_url) as redis:
        await admin.run(lambda db: db.execute(f"CREATE SCHEMA {schema}"))
        try:
            async with storage.pool:
                await storage.run(lambda db: migrate(db, seed=True, clock=clock))
                return await run_workload(storage, settings, clock, concurrency, repetitions, fault)
        finally:
            await admin.run(lambda db: db.execute(f"DROP SCHEMA {schema} CASCADE"))
            keys = [key async for key in redis.scan_iter(f"{schema}:*")]
            if keys:
                await redis.delete(*keys)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--no-faults", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 40 or not 1 <= args.repetitions <= 40:
        parser.error("--concurrency and --repetitions must be between 1 and 40")
    database = os.environ.get(
        "DATABASE_URL", "postgresql://aftersales:aftersales_dev@127.0.0.1:5432/aftersales"
    )
    redis_url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        result = runner.run(
            run(database, redis_url, args.concurrency, args.repetitions, not args.no_faults)
        )
    report = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report + "\n", encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()

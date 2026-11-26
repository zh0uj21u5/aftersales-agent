"""M8 concurrency, provider wait boundaries, SSE and incremental read contracts."""

import asyncio
import json
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from starlette.types import Message

from aftersales.core.config import AgentConfig
from aftersales.core.llm import AsyncMockLLM, LLMResponse, ToolCall, Usage, run_sync
from aftersales.core.locks import InMemorySessionLock
from aftersales.core.loop import Session
from aftersales.core.session_store import SessionStore
from aftersales.core.tracing import read_trace
from aftersales.domain.clock import FixedClock
from aftersales.domain.db import connect, dump_hashes, ensure_read_indexes
from aftersales.domain.seed import seed_database
from aftersales.domain.services import Services
from aftersales.web.app import create_app

PRICE_ARGS = {"order_id": "O1006", "order_item_id": "OI00006"}


def using(name: str, **arguments: Any) -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id=name, name=name, arguments=arguments)], stop_reason="tool_use"
    )


def proposal_script() -> list[LLMResponse]:
    return [
        using(
            "propose_actions",
            actions=[{"tool_name": "apply_price_protection", "arguments": PRICE_ARGS}],
        ),
        LLMResponse(text="O1006可以原路退差57.00元，请确认。"),
    ]


@pytest.fixture
def database(tmp_path: Path) -> Path:
    db = tmp_path / "shop.db"
    seed_database(db)
    return db


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://unit.test")


async def create(http: httpx.AsyncClient, user: str | None = None) -> str:
    response = await http.post("/api/sessions", json={"authenticated_user_id": user})
    assert response.status_code == 201
    return "/api/sessions/" + response.json()["session_id"]


def frames(text: str) -> list[tuple[str, Any]]:
    return [
        (name.removeprefix("event: "), json.loads(data.removeprefix("data: ")))
        for frame in text.strip().split("\n\n")
        if frame and not frame.startswith(":")
        for name, data in [frame.splitlines()]
    ]


async def raw_sse(
    app: FastAPI,
    path: str,
    content: str,
    disconnected: asyncio.Event,
    sent: Callable[[Message], None],
) -> None:
    """Drive the ASGI stream without an HTTP transport that buffers response bodies."""
    first = True

    async def receive() -> Message:
        nonlocal first
        if first:
            first = False
            return {
                "type": "http.request",
                "body": json.dumps({"content": content}).encode(),
                "more_body": False,
            }
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "path": path,
            "root_path": "",
            "scheme": "http",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
        send,
    )


def test_ten_async_sessions_share_wall_time(database: Path) -> None:
    class SlowMock(AsyncMockLLM):
        active = peak = 0

        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            self.active += 1
            self.peak = max(self.active, self.peak)
            try:
                return await super().chat(*args, **kwargs)
            finally:
                self.active -= 1

    async def check() -> None:
        mock = SlowMock([LLMResponse(text="查询收到")] * 10, delay=0.3)
        async with client(create_app(database, mock, trace_dir=database.parent / "traces")) as http:
            paths = [await create(http) for _ in range(10)]
            start = time.perf_counter()
            responses = await asyncio.gather(
                *(
                    http.post(path + "/messages", json={"content": f"会话 {i}"})
                    for i, path in enumerate(paths)
                )
            )
            elapsed = time.perf_counter() - start
        assert mock.peak == 10
        print(f"10 sessions, 0.3 s per model call, wall time {elapsed:.3f} s")
        assert elapsed < 1.5
        for i, response in enumerate(responses):
            assert response.status_code == 200
            assert [m["content"] for m in response.json()["session"]["messages"]] == [
                f"会话 {i}",
                "查询收到",
            ]

    run_sync(check())


def test_anonymous_view_skips_order_and_approval_queries(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statements: list[str] = []

    def tracked(path: Path) -> sqlite3.Connection:
        db = connect(path)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr("aftersales.web.app.connect", tracked)

    async def check() -> None:
        async with client(create_app(database, AsyncMockLLM([]))) as http:
            path = await create(http)
            response = await http.get(path)
            assert response.json()["orders"] == response.json()["approvals"] == []

    run_sync(check())
    assert not any(
        sql.lstrip().upper().startswith("SELECT")
        and sql.split("FROM")[-1].strip().startswith(("orders", "approvals"))
        for sql in statements
    )


def test_immediate_lock_uses_actual_availability() -> None:
    async def check() -> None:
        locks = InMemorySessionLock()
        async with locks.acquire("session"):
            # Model a cancelled waiter still unwinding while the lock becomes available.
            entry = locks._entries["session"]
            entry.users += 1
        async with locks.acquire("session", timeout=-1):
            assert entry.lock.locked()

    run_sync(check())


def test_same_session_mutations_conflict_during_model_wait(database: Path) -> None:
    async def check() -> None:
        entered, proceed = asyncio.Event(), asyncio.Event()

        class PausedMock:
            async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
                entered.set()
                await proceed.wait()
                return LLMResponse(text="回复一")

        app = create_app(database, PausedMock(), trace_dir=database.parent / "traces")
        async with client(app) as http:
            path = await create(http)
            first = asyncio.create_task(http.post(path + "/messages", json={"content": "请求一"}))
            try:
                await asyncio.wait_for(entered.wait(), timeout=2)
                for suffix, payload in (
                    ("/messages", {"content": "请求二"}),
                    ("/messages/stream", {"content": "请求三"}),
                    ("/demo/advance", {}),
                    ("/proposals/P0001/decision", {"version": 1, "decision": "approve"}),
                ):
                    denied = await http.post(path + suffix, json=payload)
                    assert denied.status_code == 409, denied.text
                    assert denied.json()["detail"] == "当前会话正在处理上一条消息"
                # A read request can use its own short connection during the suspended model.
                assert (await http.get(path)).status_code == 200
            finally:
                proceed.set()
            response = await first
            assert response.status_code == 200
            assert [m["content"] for m in response.json()["session"]["messages"]] == [
                "请求一",
                "回复一",
            ]

    run_sync(check())


def test_all_model_waits_have_no_open_connection(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    alive: set[sqlite3.Connection] = set()

    class Tracked(sqlite3.Connection):
        def close(self) -> None:
            alive.remove(self)
            super().close()

    def tracked_connect(path: Path) -> sqlite3.Connection:
        db = sqlite3.connect(path, isolation_level=None, factory=Tracked)
        alive.add(db)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    monkeypatch.setattr("aftersales.web.app.connect", tracked_connect)

    class AuditedMock(AsyncMockLLM):
        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            assert not any(db.in_transaction for db in alive)
            assert not alive
            await asyncio.sleep(0)
            assert not alive
            return await super().chat(*args, **kwargs)

    async def check() -> None:
        # O1006: price drop (13000 - 10000) * 2 * 24700 / 26000 = 5700 cents.
        mock = AuditedMock(
            [
                *proposal_script(),
                LLMResponse(text="APPROVE"),
                using("apply_price_protection", **PRICE_ARGS),
                LLMResponse(text="已退款57.00元。", usage=Usage(input_tokens=10, cached_tokens=4)),
                LLMResponse(text='{"complete":true,"missing":[]}'),
            ]
        )
        app = create_app(
            database,
            mock,
            AgentConfig(proposal_gate=True, confirm_mode="llm", goal_judge=True),
            trace_dir=database.parent / "traces",
        )
        async with client(app) as http:
            path = await create(http, "U006")
            first = await http.post(path + "/messages", json={"content": "申请价保"})
            assert first.json()["session"]["proposals"][-1]["status"] == "pending"
            second = await http.post(path + "/messages", json={"content": "确认"})
        response = second.json()
        assert response["turn"]["reply"] == "已退款57.00元。"
        assert len(response["session"]["action_executions"]) == 1
        calls = [
            event["data"]
            for event in response["session"]["trace_events"]
            if event["event"] == "llm_call"
        ]
        assert [data["purpose"] for data in calls] == [
            "confirmation",
            "agent",
            "agent",
            "goal_judge",
        ]
        assert (
            response["turn"]["usage"]["cached_tokens"] == calls[2]["tokens"]["cached_tokens"] == 4
        )
        assert not alive and len(mock.requests) == 6

    run_sync(check())


def test_stream_guard_proposal_final_frame_and_headers(database: Path) -> None:
    async def check() -> None:
        mock = AsyncMockLLM([using("get_order", order_id="O1005"), *proposal_script()])
        async with client(create_app(database, mock, trace_dir=database.parent / "traces")) as http:
            path = await create(http, "U006")
            response = await http.post(path + "/messages/stream", json={"content": "请求价保"})
            assert response.status_code == 200
            assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
            assert response.headers["cache-control"] == "no-cache"
            assert response.headers["x-accel-buffering"] == "no"
            events = frames(response.text)
            kinds = [kind for kind, _ in events]
            assert kinds == [
                "turn_start",
                "tool_call",
                "guard_verdict",
                "tool_result",
                "tool_call",
                "guard_verdict",
                "tool_result",
                "proposal_created",
                "assistant_delta",
                "turn_end",
            ]
            assert events[2][1]["allowed"] is False and events[2][1]["rule_ids"] == ["P01"]
            assert events[7][1]["proposal"]["status"] == "pending"
            assert events[8][1]["text"] == events[-1][1]["turn"]["reply"]
            assert (await http.get(path)).json() == events[-1][1]["session"]

    run_sync(check())


def test_live_delta_precedes_completion_and_disconnect_releases_session(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aftersales.web.app.HEARTBEAT_SECONDS", 0.01)

    async def check() -> None:
        received_delta, received_heartbeat, disconnected, cancelled = (
            asyncio.Event() for _ in range(4)
        )

        class StreamingMock:
            async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
                return LLMResponse(text="重新连接成功")

            async def stream_chat(self, *args: Any) -> LLMResponse:
                on_delta = args[-1]
                on_delta("正在生成")
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

        traces = database.parent / "traces"
        app = create_app(database, StreamingMock(), trace_dir=traces)
        async with client(app) as http:
            path = await create(http)

            def sink(message: Message) -> None:
                body = message.get("body", b"").decode("utf-8")
                if "正在生成" in body and "assistant_delta" in body:
                    received_delta.set()
                if body == ": keep-alive\n\n":
                    received_heartbeat.set()

            request = asyncio.create_task(
                raw_sse(app, path + "/messages/stream", "开场", disconnected, sink)
            )
            try:
                await asyncio.wait_for(received_delta.wait(), 2)
                await asyncio.wait_for(received_heartbeat.wait(), 2)
                assert not request.done()
            finally:
                disconnected.set()
                await asyncio.wait_for(request, 2)
            assert cancelled.is_set()
            trace = read_trace(next(traces.glob("*.jsonl")))
            assert trace[-1].data["reason"] == "disconnected"
            resumed = await http.post(path + "/messages", json={"content": "继续"})
            assert resumed.status_code == 200
            assert [m["content"] for m in resumed.json()["session"]["messages"]] == [
                "开场",
                "继续",
                "重新连接成功",
            ]

    run_sync(check())


def test_disconnect_after_action_commit_preserves_one_refund(database: Path) -> None:
    async def check() -> None:
        mock = AsyncMockLLM(
            [
                *proposal_script(),
                using("apply_price_protection", **PRICE_ARGS),
                LLMResponse(text="继续查询。"),
            ]
        )
        disconnected, committed, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class PauseAfterWrite:
            async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
                if len(mock.requests) == 3 and not disconnected.is_set():
                    try:
                        await asyncio.Future()
                    finally:
                        cancelled.set()
                return await mock.chat(*args, **kwargs)

        app = create_app(database, PauseAfterWrite(), trace_dir=database.parent / "traces")
        async with client(app) as http:
            path = await create(http, "U006")
            await http.post(path + "/messages", json={"content": "申请价保"})

            def sink(message: Message) -> None:
                if message.get("body", b"").startswith(b"event: tool_result"):
                    committed.set()

            request = asyncio.create_task(
                raw_sse(app, path + "/messages/stream", "确认", disconnected, sink)
            )
            try:
                await asyncio.wait_for(committed.wait(), 2)
            finally:
                disconnected.set()
                await asyncio.wait_for(request, 2)
            assert cancelled.is_set()
            response = await http.post(path + "/messages", json={"content": "继续查询"})
            snapshot = response.json()["session"]
            assert response.status_code == 200
            assert len(snapshot["action_executions"]) == 1
            assert snapshot["proposals"][-1]["status"] == "consumed"
            # O1006: the one committed refund is the 5700-cent coupon-adjusted price drop.
            with closing(connect(database)) as db:
                refunds = db.execute(
                    "SELECT amount_cents FROM refunds WHERE order_id='O1006'"
                ).fetchall()
                assert [row[0] for row in refunds] == [5700]

    run_sync(check())


def test_trace_cursor_resume_and_messages_only_reads_current_run(
    database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aftersales.web.app import iter_trace

    reads: list[str] = []

    def track(path: Path, **options: Any) -> Any:
        reads.append(path.stem)
        return iter_trace(path, **options)

    monkeypatch.setattr("aftersales.web.app.iter_trace", track)

    async def check() -> None:
        trace_dir = database.parent / "traces"
        mock = AsyncMockLLM([LLMResponse(text="答复")] * 3)
        async with client(create_app(database, mock, trace_dir=trace_dir)) as http:
            path = await create(http)
            first = (await http.post(path + "/messages", json={"content": "第一轮"})).json()
            reads.clear()
            second = (await http.post(path + "/messages", json={"content": "第二轮"})).json()
            first_run = first["session"]["trace_events"][0]["run_id"]
            second_run = second["session"]["trace_events"][0]["run_id"]
            assert reads == [second_run] and first_run != second_run
            expected = first["session"]["trace_events"] + second["session"]["trace_events"]
            cursor = 0
            pages = []
            while True:
                response = await http.get(
                    path + "/traces", params={"after_seq": cursor, "limit": 2}
                )
                page = response.json()
                pages.extend(page["events"])
                cursor = page["next_seq"]
                if not page["has_more"]:
                    break
            assert pages == expected and cursor == expected[-1]["seq"]
        async with client(create_app(database, mock, trace_dir=trace_dir)) as http:
            assert (await http.get(path + "/traces", params={"after_seq": cursor})).json() == {
                "events": [],
                "next_seq": cursor,
                "has_more": False,
            }
            third = (await http.post(path + "/messages", json={"content": "第三轮"})).json()
            reads.clear()
            assert (await http.get(path + "/traces", params={"after_seq": cursor})).json()[
                "events"
            ] == third["session"]["trace_events"]
            assert first_run not in reads
            assert (await http.get("/api/sessions/unknown/traces")).status_code == 404
            assert (await http.get(path + "/traces?limit=0")).status_code == 422

    run_sync(check())


def test_batch_snapshot_view_and_legacy_confirmation(shop: Services) -> None:
    ids = [order.id for order in shop.list_orders("U006")]
    expected = [shop.action_snapshot(order_id) for order_id in ids]
    queries: list[str] = []
    shop.db.set_trace_callback(queries.append)
    assert shop.batch_action_snapshots(ids) == expected
    assert shop.batch_action_snapshots([]) == []
    assert sum(query.startswith("SELECT") for query in queries) <= 7
    shop.db.set_trace_callback(None)
    before = dump_hashes(shop.db)
    store = SessionStore(shop.db, FixedClock())
    session_id = "legacy"
    old = {
        "session_id": session_id,
        "presented_proposal_version": 4,
        "confirmation_user_index": 3,
        "confirmation_available": True,
        "confirmation_checked": True,
        "confirmation_identity": "U006",
        "confirmation_proposal": "已展示的方案",
        "confirmed_batch": True,
    }
    shop.db.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?)", (session_id, json.dumps(old), 1, "old")
    )
    restored = store.load(session_id)
    assert restored.confirmation.model_dump() == {
        "presented_version": 4,
        "user_index": 3,
        "available": True,
        "checked": True,
        "identity": "U006",
        "presented_text": "已展示的方案",
        "batch_open": True,
    }
    store.save(restored)
    assert "confirmation_user_index" not in restored.model_dump()
    assert (
        restored.confirmation
        == Session.model_validate_json(restored.model_dump_json()).confirmation
    )
    assert dump_hashes(shop.db) == before


def test_legacy_trace_ordinals_survive_vacuum_and_index_upgrade(database: Path) -> None:
    with closing(connect(database)) as db:
        clock = FixedClock()
        store = SessionStore(db, clock)
        session = Session()
        store.save(session)
        db.execute("DROP TABLE session_traces")
        db.execute("""CREATE TABLE session_traces (
            session_id TEXT NOT NULL REFERENCES sessions(session_id), run_id TEXT NOT NULL,
            PRIMARY KEY(session_id, run_id))""")
        db.execute(
            "INSERT INTO session_traces (rowid, session_id, run_id) VALUES (7, ?, 'old')",
            (session.session_id,),
        )
        before = dump_hashes(db)
        db.execute("DROP INDEX idx_orders_user_created")
        ensure_read_indexes(db)
        upgraded = SessionStore(db, clock)
        assert upgraded.trace_runs(session.session_id) == [(7, "old")]
        assert upgraded.attach_trace(session.session_id, "new") == 8
        db.execute("VACUUM")
        assert upgraded.trace_runs(session.session_id) == [(7, "old"), (8, "new")]
        assert dump_hashes(db) == before


def test_partial_return_is_exposed_only_in_order_view(database: Path) -> None:
    from fastapi.testclient import TestClient

    # O1005 bought 3 pieces. Returning one leaves 2 available, so 0 < 1 < 3.
    with closing(connect(database)) as db:
        service = Services(db, FixedClock())
        db.execute("UPDATE order_items SET returned_qty=1, status='returned' WHERE id='OI00005'")
        raw = service.action_snapshot("O1005").order_items[0]
        assert raw["status"] == "returned" and "is_partially_returned" not in raw
    http = TestClient(create_app(database, AsyncMockLLM([]), trace_dir=database.parent / "traces"))
    response = http.post("/api/sessions", json={"authenticated_user_id": "U005"})
    order = next(
        order for order in response.json()["orders"] if order["orders"][0]["id"] == "O1005"
    )
    assert order["order_items"][0]["status"] == "returned"
    assert order["order_items"][0]["is_partially_returned"] is True

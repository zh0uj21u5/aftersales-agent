"""Real SQLite transactions and explicit recovery retries; no network calls."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from aftersales.core import execution
from aftersales.core.config import AgentConfig
from aftersales.core.execution import execute_action, execute_proposal, list_executions
from aftersales.core.llm import Message, ToolCall
from aftersales.core.loop import Session
from aftersales.core.session_store import SessionStore
from aftersales.domain.clock import FixedClock
from aftersales.domain.db import TABLES, connect, dump_hashes
from aftersales.domain.seed import seed_database
from aftersales.domain.services import PolicyError, Services
from aftersales.policy.proposal import ActionRequest, create_proposal, decide_proposal
from aftersales.tools.registry import ToolContext


def authorized(shop: Services, name: str, args: dict[str, Any], user: str = "U006") -> ToolContext:
    session = Session(authenticated_user_id=user)
    ctx = ToolContext(session, AgentConfig(proposal_gate=True), shop.db, shop.clock)
    store = SessionStore(shop.db, shop.clock)
    store.save(session)
    p = create_proposal(ctx, [ActionRequest(tool_name=name, arguments=args)]).proposal
    decide_proposal(session, p.proposal_id, p.version, "approve")
    return ctx


def run(ctx: ToolContext, **changes: Any) -> Any:
    p = ctx.session.proposals[-1]
    a = p.actions[0]
    return execute_action(
        ctx, p.proposal_id, a.action_id, p.version, a.tool_name, {**a.canonical_args, **changes}
    )


@pytest.mark.parametrize(
    "name",
    [
        "modify_address",
        "cancel_order",
        "apply_price_protection",
        "request_return",
        "request_exchange",
        "create_order",
    ],
)
def test_all_six_actions_snapshot_commit_and_replay(shop: Services, name: str) -> None:
    # O1006: (13000*2+14000)-2000=38000; price refund 6000*24700/26000=5700.
    # O1005: 45000/3=15000; O1002 exchange: 13000-11000=2000; new S0001=10000.
    args: dict[str, Any] = {"order_id": "O1006", "reason": "买家要求"}
    user = "U006"
    if name == "modify_address":
        args = {
            "order_id": "O1006",
            "address": {**shop.get_order("O1006").address.model_dump(), "detail": "公司88号"},
        }
    elif name == "apply_price_protection":
        args = {"order_id": "O1006", "order_item_id": shop.order_details("O1006").items[0].id}
    elif name == "request_return":
        user = "U005"
        args = {
            "order_id": "O1005",
            "reason": "不需要",
            "items": [{"order_item_id": shop.order_details("O1005").items[0].id, "qty": 1}],
        }
    elif name == "request_exchange":
        user = "U002"
        args = {
            "order_id": "O1002",
            "order_item_id": shop.order_details("O1002").items[0].id,
            "new_sku_id": "S0002",
            "reason": "换颜色",
        }
    elif name == "create_order":
        args = {
            "user_id": user,
            "items": [{"sku_id": "S0001", "qty": 1}],
            "payment_method_id": "PM0006",
        }
    ctx = authorized(shop, name, args, user)
    session = ctx.session
    session_before = session.model_copy(deep=True)
    commands: list[str] = []
    shop.db.set_trace_callback(commands.append)
    result = run(ctx)
    shop.db.set_trace_callback(None)
    assert commands[0] == "BEGIN IMMEDIATE"
    assert "SAVEPOINT domain_write" in commands and commands[-1] == "COMMIT"
    assert session.proposals[-1].status == "consumed"
    record = list_executions(session._store, session.session_id)[0]
    assert record.result == result
    assert record.before_snapshot != record.after_snapshot
    assert record.created_at == shop.clock.now().isoformat()
    assert session._store.load(session.session_id).model_dump() == session.model_dump()
    if name == "create_order":
        assert record.before_snapshot.orders == []
        assert record.after_snapshot.orders[0]["id"] == result.order_id
        assert record.before_snapshot.skus[0]["stock"] - 1 == record.after_snapshot.skus[0]["stock"]
    elif name == "apply_price_protection":
        assert record.before_snapshot.orders[0]["price_protected"] == 0
        assert record.after_snapshot.orders[0]["price_protected"] == 1
        assert record.after_snapshot.refunds[-1]["amount_cents"] == 5700
        assert record.after_snapshot.aftersales[-1]["id"] in result.record_ids
    elif name == "modify_address":
        assert record.after_snapshot.orders[0]["address"]["detail"] == "公司88号"
    elif name == "cancel_order":
        assert record.after_snapshot.orders[0]["status"] == "cancelled"
        assert record.after_snapshot.user_coupons[0]["status"] in {"unused", "expired"}
    before_replay = dump_hashes(shop.db)
    # Simulate a process that still holds the pre-commit authorization state.
    retry_ctx = ToolContext(session_before, ctx.config, shop.db, shop.clock)
    assert run(retry_ctx) == result
    assert retry_ctx.session.proposals[-1].status == "consumed"
    assert retry_ctx.session.proposals[-1].actions[0].status == "executed"
    assert dump_hashes(shop.db) == before_replay
    assert len(list_executions(session._store, session.session_id)) == 1
    assert tuple(dump_hashes(shop.db)) == TABLES and len(TABLES) == 14


def price_context(shop: Services) -> ToolContext:
    return authorized(
        shop,
        "apply_price_protection",
        {"order_id": "O1006", "order_item_id": shop.order_details("O1006").items[0].id},
    )


def test_commit_succeeds_but_response_and_memory_sync_are_lost(
    shop: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    # O1006 refund remains 5700 once, even when synchronization after COMMIT raises.
    ctx = price_context(shop)
    with monkeypatch.context() as patch:

        def crash(*args: Any) -> None:
            raise RuntimeError("process interrupted after commit")

        patch.setattr(execution, "_sync", crash)
        with pytest.raises(RuntimeError):
            run(ctx)
    assert ctx.session.proposals[-1].status == "approved"
    assert ctx.session._store.load(ctx.session.session_id).proposals[-1].status == "consumed"
    assert run(ctx).amount_cents == 5700
    assert shop.refunded_cents("O1006") == 5700
    assert ctx.session.proposals[-1].actions[0].status == "executed"


def test_committed_checkpoint_contains_tool_reply_before_response_is_returned(
    shop: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = price_context(shop)
    action = ctx.session.proposals[-1].actions[0]
    ctx.session.messages.append(
        Message(
            role="assistant",
            tool_calls=[
                ToolCall(id="refund-call", name=action.tool_name, arguments=action.canonical_args)
            ],
        )
    )
    ctx.tool_call_id = "refund-call"
    with monkeypatch.context() as patch:

        def crash(*args: Any) -> None:
            raise RuntimeError("response lost")

        patch.setattr(execution, "_sync", crash)
        with pytest.raises(RuntimeError):
            run(ctx)
    restored = ctx.session._store.load(ctx.session.session_id)
    assert restored.messages[-1].role == "tool"
    assert restored.messages[-1].tool_call_id == "refund-call"
    assert "5700" in restored.messages[-1].content


@pytest.mark.parametrize("stage", ["business", "after_snapshot", "checkpoint", "audit_insert"])
def test_all_transaction_failure_windows_roll_back(
    shop: Services, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    ctx = price_context(shop)
    before = dump_hashes(shop.db)
    original_session = ctx.session.model_dump()
    if stage == "business":
        original = Services.apply_price_protection

        def broken(self: Services, *args: Any, **kwargs: Any) -> Any:
            original(self, *args, **kwargs)
            raise RuntimeError("after business savepoint released")

        monkeypatch.setattr(Services, "apply_price_protection", broken)
    elif stage == "after_snapshot":
        snapshot = Services.action_snapshot
        count = 0

        def broken_snapshot(self: Services, *args: Any, **kwargs: Any) -> Any:
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("snapshot failed")
            return snapshot(self, *args, **kwargs)

        monkeypatch.setattr(Services, "action_snapshot", broken_snapshot)
    elif stage == "checkpoint":
        save = SessionStore.save

        def broken_save(self: SessionStore, session: Session) -> None:
            save(self, session)
            raise RuntimeError("checkpoint failed")

        monkeypatch.setattr(SessionStore, "save", broken_save)
    else:
        shop.db.execute("""CREATE TRIGGER fail_audit BEFORE INSERT ON action_executions
                           BEGIN SELECT RAISE(ABORT, 'audit failed'); END""")
    with pytest.raises((RuntimeError, sqlite3.IntegrityError)):
        run(ctx)
    assert not shop.db.in_transaction
    assert dump_hashes(shop.db) == before
    assert ctx.session.model_dump() == original_session
    assert ctx.session._store.load(ctx.session.session_id).model_dump() == original_session
    assert list_executions(ctx.session._store, ctx.session.session_id) == []


def test_same_key_changed_business_parameters_or_tool_are_p02(shop: Services) -> None:
    ctx = price_context(shop)
    run(ctx)
    before = dump_hashes(shop.db)
    with pytest.raises(PolicyError, match="幂等键参数冲突") as exc:
        run(ctx, order_item_id=shop.order_details("O1006").items[1].id)
    assert exc.value.rule_ids == ["P02"]
    p = ctx.session.proposals[-1]
    with pytest.raises(PolicyError, match="幂等键参数冲突"):
        execute_action(
            ctx,
            p.proposal_id,
            p.actions[0].action_id,
            p.version,
            "cancel_order",
            {"order_id": "O1006", "reason": "篡改工具"},
        )
    assert dump_hashes(shop.db) == before


def test_reason_is_not_a_business_parameter_and_identity_still_required(shop: Services) -> None:
    ctx = authorized(shop, "cancel_order", {"order_id": "O1006", "reason": "原理由"})
    result = run(ctx)
    assert run(ctx, reason="换个非空理由") == result
    ctx.session.authenticated_user_id = "U005"
    with pytest.raises(PolicyError) as exc:
        run(ctx)
    assert exc.value.rule_ids == ["P01"]


def test_approval_candidate_never_refunds(shop: Services) -> None:
    # 45000/3*3=45000 >20000: approving the card cannot bypass P08.
    ctx = authorized(
        shop,
        "request_return",
        {
            "order_id": "O1005",
            "items": [{"order_item_id": shop.order_details("O1005").items[0].id, "qty": 3}],
            "reason": "全退",
        },
        "U005",
    )
    before = dump_hashes(shop.db)
    assert execute_proposal(ctx, "P0001", 1) == []
    with pytest.raises(PolicyError) as exc:
        run(ctx)
    assert "P08" in exc.value.rule_ids
    assert dump_hashes(shop.db) == before


def test_concurrent_recovery_across_connections_commits_once(tmp_path: Path) -> None:
    # S0001 costs 10000; two recoveries of ACT01 must create one order and consume one unit.
    path = tmp_path / "shop.db"
    seed_database(path)
    db = connect(path)
    ctx = authorized(
        Services(db, FixedClock()),
        "create_order",
        {
            "user_id": "U006",
            "items": [{"sku_id": "S0001", "qty": 1}],
            "payment_method_id": "PM0006",
        },
    )
    session_id = ctx.session.session_id
    stock = db.execute("SELECT stock FROM skus WHERE id='S0001'").fetchone()[0]
    barrier = Barrier(2)

    def retry() -> Any:
        connection = connect(path)
        try:
            store = SessionStore(connection, FixedClock())
            loaded = store.load(session_id)
            barrier.wait(timeout=10)
            return run(ToolContext(loaded, ctx.config, connection, FixedClock()))
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(retry) for _ in range(2)]
        results = [f.result(timeout=20) for f in futures]
    assert results[0] == results[1]
    assert db.execute("SELECT count(*) FROM orders").fetchone()[0] == 301
    assert db.execute("SELECT stock FROM skus WHERE id='S0001'").fetchone()[0] == stock - 1
    assert len(list_executions(ctx.session._store, session_id)) == 1
    db.close()


def test_partial_plan_commit_then_retry_recovers_and_finishes_once(
    shop: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Independent address + price actions: address commits, refund 5700 rolls back, then retries once.
    ctx = price_context(shop)
    address = {**shop.get_order("O1006").address.model_dump(), "detail": "公司88号"}
    create_proposal(
        ctx,
        [
            ActionRequest(
                tool_name="modify_address", arguments={"order_id": "O1006", "address": address}
            ),
            ActionRequest(
                tool_name="apply_price_protection",
                arguments={"order_id": "O1006", "order_item_id": "OI00006"},
            ),
        ],
    )
    decide_proposal(ctx.session, "P0002", 2, "approve")
    original = Services.apply_price_protection
    with monkeypatch.context() as patch:

        def fail_after_refund(self: Services, *args: Any, **kwargs: Any) -> Any:
            original(self, *args, **kwargs)
            raise RuntimeError("interrupted second action")

        patch.setattr(Services, "apply_price_protection", fail_after_refund)
        with pytest.raises(RuntimeError):
            execute_proposal(ctx, "P0002", 2)
    restored = ctx.session._store.load(ctx.session.session_id)
    assert restored.proposals[-1].status == "approved"
    assert [a.status for a in restored.proposals[-1].actions] == ["executed", "approved"]
    assert shop.get_order("O1006").address.detail == "公司88号"
    assert shop.refunded_cents("O1006") == 0
    assert len(list_executions(ctx.session._store, restored.session_id)) == 1
    ctx.session = restored
    assert execute_proposal(ctx, "P0002", 2)[1].amount_cents == 5700
    assert len(list_executions(restored._store, restored.session_id)) == 2
    assert restored.proposals[-1].status == "consumed"
    assert shop.refunded_cents("O1006") == 5700


def test_session_namespace_isolates_identical_proposal_and_action_ids(shop: Services) -> None:
    # Each independently approved new order costs 10000 and consumes one unit; keys include Session.
    args = {
        "user_id": "U006",
        "items": [{"sku_id": "S0001", "qty": 1}],
        "payment_method_id": "PM0006",
    }
    first = authorized(shop, "create_order", args)
    first_result = run(first)
    second = authorized(shop, "create_order", args)
    second_result = run(second)
    assert first_result.order_id != second_result.order_id
    a = list_executions(first.session._store, first.session.session_id)[0]
    b = list_executions(second.session._store, second.session.session_id)[0]
    assert a.proposal_id == b.proposal_id and a.action_id == b.action_id
    assert a.execution_key != b.execution_key


def test_existing_key_invalid_schema_is_also_p02(shop: Services) -> None:
    ctx = price_context(shop)
    run(ctx)
    with pytest.raises(PolicyError, match="幂等键参数冲突") as exc:
        run(ctx, extra="not permitted")
    assert exc.value.rule_ids == ["P02"]

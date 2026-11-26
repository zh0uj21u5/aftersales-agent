"""Exercise real SQLite drift and the full trial grader with scripted models."""

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from aftersales.core.config import AgentConfig
from aftersales.core.llm import LLMResponse, MockLLM, ToolCall
from aftersales.core.tracing import read_trace, trace_path
from aftersales.domain.clock import FixedClock
from aftersales.domain.db import dump_hashes
from aftersales.domain.seed import INJECTION
from aftersales.domain.services import Services
from evals.models import EvalTask, load_tasks
from evals.replay import fresh_database, replay_gold
from evals.runner import dataset_sha256, run_trial
from evals.world import (
    SetCouponStatus,
    SetSkuPrice,
    SetSkuStock,
    ShipOrder,
    WorldEvent,
    WorldEventConflict,
    apply_world_event,
)

ROOT = Path(__file__).resolve().parents[1]
TASKS = load_tasks(ROOT / "evals/tasks_drift")
EVENT = TypeAdapter(WorldEvent)


@pytest.mark.parametrize(
    ("directory", "digest"),
    [
        ("tasks", "98dad7a1c8a9b0dea033e58dc34c213f71ab845b0d3aaf7570fc4b5594c16a1d"),
        ("tasks_v2", "7b5004cbc6e868b6ed3f68b02756b3534a13cd270d015f503fea2763538f3689"),
    ],
)
def test_frozen_dataset_hash(directory: str, digest: str) -> None:
    tasks = load_tasks(ROOT / "evals" / directory)
    assert all("world_events" not in task.model_dump() for task in tasks)
    assert dataset_sha256(tasks) == digest


@pytest.mark.parametrize(
    "override",
    [
        {"after_turn": 0},
        {"after_turn": 20},
        {"stock": -1},
        {"stock": "0"},
        {"price_cents": 1},
    ],
)
def test_world_event_rejects_invalid_input(override: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        EVENT.validate_python(
            {"kind": "set_sku_stock", "after_turn": 1, "sku_id": "S0052", "stock": 0} | override
        )


def test_ship_order_sets_clock_and_inserts_shipment(shop: Services) -> None:
    event = ShipOrder(kind="ship_order", after_turn=1, order_id="O1016")
    apply_world_event(shop.db, shop.clock, event)
    assert tuple(
        shop.db.execute("SELECT status, shipped_at FROM orders WHERE id='O1016'").fetchone()
    ) == (
        "shipped",
        shop.clock.now().isoformat(),
    )
    assert tuple(
        shop.db.execute(
            "SELECT tracking_no, status FROM shipments WHERE order_id='O1016'"
        ).fetchone()
    ) == (
        "T99990001",
        "shipped",
    )
    with pytest.raises(WorldEventConflict):
        apply_world_event(shop.db, shop.clock, event)


def test_ship_order_rolls_back_if_tracking_conflicts(shop: Services) -> None:
    apply_world_event(
        shop.db, shop.clock, ShipOrder(kind="ship_order", after_turn=1, order_id="O1016")
    )
    before = dump_hashes(shop.db)
    with pytest.raises(sqlite3.IntegrityError):
        apply_world_event(
            shop.db, shop.clock, ShipOrder(kind="ship_order", after_turn=1, order_id="O1058")
        )
    assert dump_hashes(shop.db) == before


def test_set_sku_stock_updates_only_requested_sku(shop: Services) -> None:
    event = SetSkuStock(kind="set_sku_stock", after_turn=1, sku_id="S0052", stock=0)
    apply_world_event(shop.db, shop.clock, event)
    assert [
        row[0]
        for row in shop.db.execute(
            "SELECT stock FROM skus WHERE id IN ('S0049','S0052') ORDER BY id"
        )
    ] == [990, 0]


def test_set_sku_price_preserves_history_and_uses_clock(shop: Services) -> None:
    event = SetSkuPrice(kind="set_sku_price", after_turn=1, sku_id="S0049", price_cents=5600)
    old = list(shop.db.execute("SELECT price_cents FROM price_history WHERE sku_id='S0049'"))
    apply_world_event(shop.db, shop.clock, event)
    assert shop.db.execute("SELECT price_cents FROM skus WHERE id='S0049'").fetchone()[0] == 5600
    history = list(
        shop.db.execute(
            "SELECT price_cents, effective_at FROM price_history WHERE sku_id='S0049' ORDER BY effective_at"
        )
    )
    assert tuple(history[-1]) == (5600, shop.clock.now().isoformat())
    assert [row[0] for row in history[:-1]] == [row[0] for row in old]


@pytest.mark.parametrize("status", ["expired", "used"])
def test_coupon_status_preserves_foreign_key_contract(shop: Services, status: str) -> None:
    order_id = "O1012" if status == "used" else None
    event = EVENT.validate_python(
        {
            "kind": "set_coupon_status",
            "after_turn": 1,
            "user_coupon_id": "UC0023",
            "status": status,
            "used_order_id": order_id,
        }
    )
    apply_world_event(shop.db, shop.clock, event)
    row = shop.db.execute(
        "SELECT status, used_order_id FROM user_coupons WHERE id='UC0023'"
    ).fetchone()
    assert tuple(row) == (status, order_id)
    assert list(shop.db.execute("PRAGMA foreign_key_check")) == []


def test_used_coupon_cannot_reference_another_buyers_order(shop: Services) -> None:
    event = SetCouponStatus(
        kind="set_coupon_status",
        after_turn=1,
        user_coupon_id="UC0023",
        status="used",
        used_order_id="O1058",
    )
    before = dump_hashes(shop.db)
    with pytest.raises(WorldEventConflict):
        apply_world_event(shop.db, shop.clock, event)
    assert dump_hashes(shop.db) == before


def test_gold_applies_world_events_in_turn_order() -> None:
    base = TASKS[3]
    first = SetSkuStock(kind="set_sku_stock", after_turn=1, sku_id="S0052", stock=12)
    last = first.model_copy(update={"after_turn": 2, "stock": 8})
    task = base.model_copy(update={"world_events": [last, *base.world_events, first]})
    with closing(fresh_database()) as db:
        result = replay_gold(Services(db, FixedClock()), task)[0]
        # One unit at 7600 cents, new price 5600, no coupon: 7600 - 5600 = 2000.
        assert result.amount_cents == 2000
        assert db.execute("SELECT stock FROM skus WHERE id='S0052'").fetchone()[0] == 8
    assert len(task.model_dump()["world_events"]) == 3
    assert EvalTask.model_validate(task.model_dump()) == task
    assert dataset_sha256([base]) != dataset_sha256([task])


def action(tool_name: str, **args: Any) -> dict[str, Any]:
    return {"tool_name": tool_name, "arguments": args}


def address(order_id: str, task: EvalTask) -> dict[str, Any]:
    return action(
        "modify_address",
        order_id=order_id,
        address={
            "receiver_name": task.user_profile.name,
            "phone": task.user_profile.phone,
            "province": "上海市",
            "city": "上海市",
            "district": "浦东新区",
            "detail": "世纪大道100号",
        },
    )


def use(*actions: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        tool_calls=[
            ToolCall(id=f"call-{i}", name=item["tool_name"], arguments=item["arguments"])
            for i, item in enumerate(actions)
        ],
    )


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.id[:3])
def test_drift_trials_block_old_proposal_then_match_gold(task: EvalTask, tmp_path: Path) -> None:
    gold = task.gold_actions[0]
    replacement = action(
        gold.tool,
        **{
            k: v for k, v in gold.args.items() if gold.tool != "transfer_to_human" or k != "user_id"
        },
    )
    old_actions = {
        "d01": [action("cancel_order", order_id="O1016", reason="买错了")],
        "d02": [address("O1058", task)],
        "d03": [
            action(
                "request_exchange",
                order_id="O1012",
                order_item_id="OI00016",
                new_sku_id="S0052",
                reason="换颜色",
            )
        ],
        "d04": [replacement],
        "d05": [action("create_order", **(gold.args | {"user_coupon_id": "UC0023"}))],
        "d06": [address("O1072", task), replacement],
    }[task.id[:3]]
    # Cancel: paid 40800. Exchange has no top-up; return 7600 for the only unit.
    # Price protection: 7600 - 4600 = 3000, then 7600 - 5600 = 2000.
    # Purchase: 13100 * 2 - 2000 = 24200, then 26200 with no coupon.
    # Compound: (17100 - 14100) * 2 = 6000, unchanged by shipment.
    original_amounts = {
        "d01": [40800],
        "d02": [None],
        "d03": [0],
        "d04": [3000],
        "d05": [24200],
        "d06": [None, 6000],
    }
    script = [
        use(action("find_user_by_phone", **task.user_profile.model_dump())),
        use(action("propose_actions", actions=old_actions)),
        LLMResponse(text="请确认服务端方案。"),
        LLMResponse(text="APPROVE"),
        use(*old_actions),
    ]
    if task.expect_handoff:
        script.append(use(replacement))
    else:
        script.extend(
            [
                use(action("propose_actions", actions=[replacement])),
                LLMResponse(text="情况已有变化，请核对并确认新的方案。"),
                LLMResponse(text="APPROVE"),
                use(replacement),
                LLMResponse(text=f"已办理，金额 ¥{task.must_mention[0]}。"),
            ]
        )
    mock = MockLLM(script)
    config = AgentConfig.model_validate(
        yaml.safe_load((ROOT / "configs/output_guard.yaml").read_bytes())
    )
    result = run_trial(task, mock, config, trace_dir=tmp_path)
    assert not mock.script and result.grade.passed and result.grade.state_correct
    assert [call.name for call in result.successful_actions] == [gold.tool]
    assert result.confirmation_blocks == len(old_actions) and result.unconfirmed_writes == 0
    events = read_trace(trace_path(tmp_path, result.trace_id))
    first_card = next(e for e in events if e.event == "proposal_created")
    assert [
        a["preview_amount_cents"] for a in first_card.data["proposal"]["actions"]
    ] == original_amounts[task.id[:3]]
    users = [e for e in events if e.event == "user_msg" and "turn_id" in e.data]
    world = next(e for e in events if e.event == "world_event")
    blocks = [e for e in events if e.event == "confirm_verdict" and e.data["allowed"] is False]
    assert users[0].seq < first_card.seq < world.seq < users[1].seq < blocks[0].seq
    assert world.data["after_turn"] == 1 and world.data["applied"] is True
    assert all(e.data["proposal_id"] == "P0001" for e in blocks)
    assert "业务前提" in blocks[0].data["reason"]
    denied_ids = {e.data["execution_id"] for e in blocks}
    assert {
        e.data["execution_id"] for e in events if e.event == "tool_result" and e.data["is_error"]
    } == denied_ids
    if not task.expect_handoff:
        approval = next(e for e in events if e.event == "confirm_verdict" and e.data["allowed"])
        assert approval.data["proposal_id"] == "P0002" and approval.seq > users[2].seq


def test_drift_tasks_only_reference_clean_seed_data() -> None:
    with closing(fresh_database()) as db:
        for task in TASKS:
            for order_id in set(re.findall(r"\bO\d{4}\b", task.opening_message)):
                assert order_id >= "O1010"
                order = db.execute("SELECT note FROM orders WHERE id=?", (order_id,)).fetchone()
                assert INJECTION not in order["note"]
                rows = db.execute(
                    """SELECT s.spu_id, p.description FROM order_items i
                    JOIN skus s ON i.sku_id=s.id JOIN spus p ON s.spu_id=p.id WHERE i.order_id=?""",
                    (order_id,),
                )
                assert all(
                    row["spu_id"] >= "SPU06" and INJECTION not in row["description"] for row in rows
                )
            for sku_id in set(re.findall(r"\bS\d{4}\b", task.opening_message)):
                row = db.execute(
                    "SELECT s.spu_id, p.description FROM skus s JOIN spus p ON s.spu_id=p.id WHERE s.id=?",
                    (sku_id,),
                ).fetchone()
                assert row["spu_id"] >= "SPU06" and INJECTION not in row["description"]

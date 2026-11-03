"""Business tool contracts, read-only previews and safe utility boundaries."""

import json
import sqlite3

import pytest

from aftersales.core.config import AgentConfig
from aftersales.core.loop import Session
from aftersales.domain.db import dump_hashes
from aftersales.domain.services import Services
from aftersales.policy.document import lookup_topic
from aftersales.tools import build_registry
from aftersales.tools.registry import ToolContext


def test_auth_success_and_failure_clear_identity(shop: Services) -> None:
    config = AgentConfig()
    registry = build_registry(config)
    session = Session()
    ctx = ToolContext(session, config, shop.db, shop.clock)
    result = registry.execute(
        "find_user_by_phone", {"phone": "13800000006", "name": "用户006"}, ctx
    )
    assert not result.is_error and session.authenticated_user_id == "U006"
    registry.execute("find_user_by_phone", {"phone": "13800000006", "name": "wrong"}, ctx)
    assert session.authenticated_user_id is None
    registry.execute("find_user_by_order", {"order_id": "O1006", "receiver_name": "用户006"}, ctx)
    assert session.authenticated_user_id == "U006"
    registry.execute("find_user_by_order", {"order_id": "O1006", "receiver_name": "wrong"}, ctx)
    assert session.authenticated_user_id is None


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_user", {"user_id": "U006"}),
        ("list_orders", {"user_id": "U006", "status": "pending_shipment"}),
        ("get_order", {"order_id": "O1006"}),
        ("get_product", {"spu_id": "SPU01"}),
        ("get_product", {"sku_id": "S0001"}),
        ("get_price_history", {"sku_id": "S0001"}),
        ("list_user_coupons", {"user_id": "U006", "status": "used"}),
    ],
)
def test_queries_are_structured_and_read_only(shop: Services, name: str, args: dict) -> None:
    before = dump_hashes(shop.db)
    config = AgentConfig()
    result = build_registry(config).execute(
        name, args, ToolContext(Session(), config, shop.db, shop.clock)
    )
    assert not result.is_error and json.loads(result.content)
    assert dump_hashes(shop.db) == before


@pytest.mark.parametrize(
    "topic,ids",
    [
        ("auth", ["P01"]),
        ("confirm", ["P02"]),
        ("address_cancel", ["P03"]),
        ("price_protection", ["P04"]),
        ("return", ["P05", "P14"]),
        ("refund", ["P06", "P07", "P15"]),
        ("approval", ["P08"]),
        ("exchange", ["P09"]),
        ("coupon", ["P10", "P13"]),
        ("security", ["P11"]),
        ("escalation", ["P12"]),
    ],
)
def test_policy_lookup_includes_all_matching_sections(topic: str, ids: list[str]) -> None:
    text = lookup_topic(topic)
    assert text.count("## P") == len(ids)
    assert all(f"## {rule} " in text for rule in ids)


@pytest.mark.parametrize(
    "expression,expected",
    [
        ("1+2*3", "7"),
        ("(1+2)*3", "9"),
        ("0.1+0.2", "0.3"),
        ("-1 / 4", "-0.25"),
        ("+2", "2"),
    ],
)
def test_calculator_exact_arithmetic(shop: Services, expression: str, expected: str) -> None:
    # Hand calculations: multiplication precedes addition; 1/4=.25; .1+.2=.3 exactly.
    ctx = ToolContext(Session(), AgentConfig(), shop.db, shop.clock)
    result = build_registry(ctx.config).execute("calculate", {"expression": expression}, ctx)
    assert not result.is_error and json.loads(result.content) == expected


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo unsafe')",
        "a",
        "2**100000",
        "[1][0]",
        "True+1",
        "1//2",
        "3%2",
        "1/0",
        "1e99999999",
        "9" * 101,
        "1+" * 300,
        "(lambda:1)()",
    ],
)
def test_calculator_rejects_code_and_excessive_work(shop: Services, expression: str) -> None:
    ctx = ToolContext(Session(), AgentConfig(), shop.db, shop.clock)
    assert build_registry(ctx.config).execute("calculate", {"expression": expression}, ctx).is_error


def test_preview_policy_off_still_reports_rules_without_mutation(shop: Services) -> None:
    # O1005: 3 * 15000 = 45000 cents, all returned -> 45000 > 20000, P08.
    item = shop.order_details("O1005").items[0]
    ctx = ToolContext(Session(), AgentConfig(guard=False), shop.db, shop.clock)
    before = dump_hashes(shop.db)
    result = build_registry(ctx.config).execute(
        "preview_refund",
        {
            "order_id": "O1005",
            "action": "request_return",
            "order_item_id": item.id,
        },
        ctx,
    )
    data = json.loads(result.content)
    assert data["amount_cents"] == 45000 and data["amount_display"] == "¥450.00"
    assert data["needs_approval"] and data["violations"][0]["rule_id"] == "P08"
    assert dump_hashes(shop.db) == before


def test_preview_feature_flag() -> None:
    assert build_registry(AgentConfig()).get("preview_refund") is not None
    assert build_registry(AgentConfig(preview_tool=False)).get("preview_refund") is None


def test_preview_cancel_and_exchange_direction(shop: Services) -> None:
    # O1006 paid 40000-2000=38000: cancellation returns 38000, exempt from P08.
    # O1002 old unit 13000, one item; replacement 15000 -> raw top-up 2000.
    before = dump_hashes(shop.db)
    preview = shop.preview_refund("O1006", "cancel_order")
    assert preview.amount_cents == 38000 and not preview.needs_approval
    assert dump_hashes(shop.db) == before
    shop.db.execute("UPDATE skus SET price_cents=15000 WHERE id='S0002'")
    before = dump_hashes(shop.db)
    preview = shop.preview_refund("O1002", "request_exchange", "OI00002", new_sku_id="S0002")
    assert preview.amount_cents == 2000 and preview.direction == "top_up" and preview.allowed
    assert dump_hashes(shop.db) == before


def test_handoff_accepts_anonymous_and_keeps_foreign_key(shop: Services) -> None:
    ctx = ToolContext(Session(), AgentConfig(), shop.db, shop.clock)
    result = build_registry(ctx.config).execute(
        "transfer_to_human", {"summary": "未认证", "reason": "用户要求"}, ctx
    )
    assert not result.is_error
    assert shop.db.execute("SELECT user_id FROM handoffs").fetchone()[0] is None
    with pytest.raises(sqlite3.IntegrityError):
        shop.transfer_to_human("U999", "unknown", "unknown")
    assert shop.db.execute("SELECT count(*) FROM handoffs").fetchone()[0] == 1


def test_all_write_wrappers_and_approval(shop: Services) -> None:
    # Each delegated service computes amounts; this test checks routes and persistence.
    config = AgentConfig(guard=False)
    ctx = ToolContext(Session(authenticated_user_id="U006"), config, shop.db, shop.clock)
    registry = build_registry(config)
    address = shop.get_order("O1006").address.model_dump()
    address["detail"] = "公司路88号"
    line = shop.order_details("O1006").items[0]
    operations = [
        ("modify_address", {"order_id": "O1006", "address": address}),
        ("apply_price_protection", {"order_id": "O1006", "order_item_id": line.id}),
        ("cancel_order", {"order_id": "O1007", "reason": "取消"}),
        (
            "request_return",
            {
                "order_id": "O1002",
                "items": [{"order_item_id": "OI00002", "qty": 1}],
                "reason": "退货",
            },
        ),
        (
            "request_exchange",
            {
                "order_id": "O1001",
                "order_item_id": "OI00001",
                "new_sku_id": "S0002",
                "reason": "换色",
            },
        ),
        (
            "create_order",
            {
                "user_id": "U006",
                "items": [{"sku_id": "S0001", "qty": 1}],
                "payment_method_id": "PM0006",
            },
        ),
        (
            "submit_approval",
            {
                "order_id": "O1005",
                "action": "request_return",
                "args": {"items": [{"order_item_id": "OI00005", "qty": 3}]},
                "reason": "大额",
            },
        ),
    ]
    for name, args in operations:
        result = registry.execute(name, args, ctx)
        assert not result.is_error, (name, result)
    assert shop.get_order("O1006").address.detail == "公司路88号"
    assert shop.get_order("O1006").price_protected
    assert shop.get_order("O1007").status == "cancelled"
    assert shop.db.execute("SELECT status FROM approvals").fetchone()[0] == "pending"


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_product", {}),
        ("get_product", {"spu_id": "SPU01", "sku_id": "S0001"}),
        ("lookup_policy", {"topic": "../private"}),
        (
            "request_return",
            {
                "order_id": "O1002",
                "items": [{"order_item_id": "OI00002", "qty": True}],
                "reason": "test",
            },
        ),
        (
            "cancel_order",
            {"order_id": "O1006", "reason": "test", "refund_payment_method_id": "PM0001"},
        ),
        ("preview_refund", {"order_id": "O1006", "action": "request_exchange"}),
    ],
)
def test_bad_tool_inputs_do_not_write(shop: Services, name: str, args: dict) -> None:
    ctx = ToolContext(Session(), AgentConfig(), shop.db, shop.clock)
    before = dump_hashes(shop.db)
    assert build_registry(ctx.config).execute(name, args, ctx).is_error
    assert dump_hashes(shop.db) == before

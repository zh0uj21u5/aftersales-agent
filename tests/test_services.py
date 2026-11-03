"""Policy examples and integer-cent arithmetic, with independent hand calculations."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest

from aftersales.domain.clock import FIXED_NOW, FixedClock
from aftersales.domain.db import connect, dump_hashes, initialize, insert, transaction
from aftersales.domain.models import (
    Address,
    ApprovalRequest,
    CheckResult,
    Coupon,
    Line,
    OrderItem,
    PaymentMethod,
    PurchaseItem,
    ReturnItem,
    UserCoupon,
)
from aftersales.domain.services import (
    PolicyError,
    Services,
    allocate_discount,
    check_approval,
    check_cancel_order,
    check_create_order,
    check_exchange,
    check_modify_address,
    check_price_protection,
    check_refund_destination,
    check_return,
    exchange_amount,
    paid_shares,
    partial_refund,
    proportional_difference,
    round_half_up,
)

ADDRESS = Address(
    receiver_name="测试用户",
    phone="13800000001",
    province="上海市",
    city="上海市",
    district="徐汇区",
    detail="测试路1号",
)


@pytest.fixture
def service() -> Iterator[Services]:
    db = connect()
    initialize(db)
    now = FIXED_NOW
    with transaction(db):
        for number in (1, 2):
            insert(
                db,
                "users",
                id=f"U{number:03}",
                name=f"用户{number}",
                phone=str(number),
                default_address=ADDRESS.model_dump_json(),
            )
            insert(
                db,
                "payment_methods",
                id=f"PM{number:04}",
                user_id=f"U{number:03}",
                type="alipay",
                label=f"账户{number}",
            )
        insert(db, "payment_methods", id="PM0003", user_id="U001", type="card", label="另一张卡")
        for number in (1, 2):
            insert(
                db,
                "spus",
                id=f"SPU{number:02}",
                title="测试商品",
                category="digital",
                description="数据",
            )
        for number, price, spu in [
            (1, 9000, "SPU01"),
            (2, 8000, "SPU01"),
            (3, 12000, "SPU01"),
            (4, 7000, "SPU02"),
        ]:
            insert(
                db, "skus", id=f"S{number:04}", spu_id=spu, attrs="{}", price_cents=price, stock=5
            )
        insert(
            db,
            "coupons",
            id="C01",
            title="满100减20",
            threshold_cents=10000,
            discount_cents=2000,
            valid_from=(now - timedelta(days=10)).isoformat(),
            valid_to=(now + timedelta(days=10)).isoformat(),
        )
        insert(
            db,
            "user_coupons",
            id="UC0001",
            user_id="U001",
            coupon_id="C01",
            status="used",
            used_order_id="O1001",
        )
        insert(
            db,
            "user_coupons",
            id="UC0002",
            user_id="U001",
            coupon_id="C01",
            status="unused",
            used_order_id=None,
        )
        insert(
            db,
            "orders",
            id="O1001",
            user_id="U001",
            status="delivered",
            address=ADDRESS.model_dump_json(),
            note="",
            created_at=(now - timedelta(days=3)).isoformat(),
            paid_at=(now - timedelta(days=2)).isoformat(),
            shipped_at=(now - timedelta(days=1, hours=1)).isoformat(),
            delivered_at=(now - timedelta(days=1)).isoformat(),
            items_total_cents=20000,
            discount_cents=2000,
            paid_cents=18000,
            payment_method_id="PM0001",
            user_coupon_id="UC0001",
            price_protected=0,
        )
        insert(
            db,
            "order_items",
            id="OI00001",
            order_id="O1001",
            sku_id="S0001",
            qty=2,
            unit_price_cents=10000,
            paid_share_cents=18000,
            status="normal",
            returned_qty=0,
            refunded_cents=0,
        )
    yield Services(db, FixedClock())
    db.close()


def rule_ids(result: CheckResult) -> set[str]:
    return {violation.rule_id for violation in result.violations}


def item(**changes: int) -> OrderItem:
    values = {
        "id": "OI1",
        "order_id": "O1",
        "sku_id": "S1",
        "qty": 3,
        "unit_price_cents": 2,
        "paid_share_cents": 5,
        "status": "normal",
        "returned_qty": 0,
        "refunded_cents": 0,
    }
    return OrderItem.model_validate({**values, **changes})


@pytest.mark.parametrize(
    "numerator,denominator,expected",
    [(0, 3, 0), (1, 2, 1), (4, 3, 1), (5, 3, 2), (10**30 + 1, 2, 5 * 10**29 + 1)],
)
def test_round_half_up(numerator: int, denominator: int, expected: int) -> None:
    # 0/3=0; 1/2=0.5 ->1; 4/3=1+1/3 ->1; 5/3=1+2/3 ->2;
    # (10^30+1)/2 = 5*10^29 + 0.5 -> 5*10^29+1, without float precision loss.
    assert round_half_up(numerator, denominator) == expected


def test_largest_remainders_tie_break_and_exact_conservation() -> None:
    # Lines 9900/9900/10000 sum to 29800. Discount 2000 produces
    # 664+12800/29800, 664+12800/29800, 671+4200/29800 cents.
    # Floors sum to 1999; the tied largest remainder goes to line 1 ->665/664/671.
    # Paid shares: 9900-665=9235; 9900-664=9236; 10000-671=9329; sum=27800.
    assert allocate_discount([9900, 9900, 10000], 2000) == [665, 664, 671]
    assert paid_shares([9900, 9900, 10000], 2000) == [9235, 9236, 9329]


@pytest.mark.parametrize("discount,expected", [(0, [0, 0]), (300, [100, 200]), (1, [0, 1])])
def test_allocation_boundaries(discount: int, expected: list[int]) -> None:
    # Lines 100+200=300. Discount 0 ->0/0; 300 ->100/200;
    # discount 1 ->1/3 and 2/3, floors 0/0, remaining cent to line 2 ->0/1.
    assert allocate_discount([100, 200], discount) == expected


@pytest.mark.parametrize(
    "lines,discount", [([], 0), ([0], 0), ([100], 101), ([100], -1), ([True], 0)]
)
def test_invalid_allocation_inputs(lines: list[int], discount: int) -> None:
    # Empty/zero/noninteger lines have no positive allocation base;
    # 101>100 exceeds the sum; -1 is not a valid discount.
    with pytest.raises(ValueError):
        allocate_discount(lines, discount)


def test_partial_refund_last_item_absorbs_rounding_and_prior_difference() -> None:
    # Qty=3, share=5: first 5/3 ->2, second 5/3 ->2, final remaining 5-2-2=1.
    assert partial_refund(item(), 1) == 2
    assert partial_refund(item(returned_qty=1, refunded_cents=2), 1) == 2
    assert partial_refund(item(returned_qty=2, refunded_cents=4), 1) == 1
    # Prior price refund=2, remaining cash=3; first return=5/3 ->2, final two=5-2-2=1.
    assert partial_refund(item(refunded_cents=2), 1) == 2
    assert partial_refund(item(returned_qty=1, refunded_cents=4), 2) == 1


@pytest.mark.parametrize("qty", [0, -1, 4, True])
def test_partial_refund_invalid_quantities(qty: int) -> None:
    # Qty must be an integer in [1,3]; 0,-1,4 and bool do not qualify.
    with pytest.raises(ValueError):
        partial_refund(item(), qty)


@pytest.mark.parametrize(
    "status,allowed",
    [
        ("pending_payment", True),
        ("pending_shipment", True),
        ("shipped", False),
        ("delivered", False),
        ("completed", False),
        ("aftersale", False),
        ("cancelled", False),
    ],
)
def test_address_cancel_state_rules(service: Services, status: str, allowed: bool) -> None:
    # Fixture paid cents = 2*10000-2000=18000; state changes do not change money.
    order = service.get_order("O1001").model_copy(update={"status": status})
    assert check_modify_address(order).allowed == allowed
    assert check_cancel_order(order).allowed == allowed


@pytest.mark.parametrize(
    "days,delivered,allowed",
    [(7, True, True), (8, True, False), (8, False, True), (30, False, True)],
)
def test_price_protection_window_and_unsigned_exception(
    service: Services,
    days: int,
    delivered: bool,
    allowed: bool,
) -> None:
    # Delta=(10000-9000)*2*18000/20000=1800 exactly. Boundary is inclusive at paid+7d.
    order = service.get_order("O1001").model_copy(
        update={
            "created_at": FIXED_NOW - timedelta(days=days + 1),
            "paid_at": FIXED_NOW - timedelta(days=days),
            "delivered_at": FIXED_NOW - timedelta(hours=1) if delivered else None,
            "status": "delivered" if delivered else "shipped",
        }
    )
    result = check_price_protection(order, service.get_line("O1001", "OI00001"), FIXED_NOW)
    assert result.allowed == allowed and result.amount_cents == 1800


@pytest.mark.parametrize("change", ["protected", "no_drop", "higher_price", "completed", "unpaid"])
def test_price_protection_rejections(service: Services, change: str) -> None:
    # Normal delta=1000*2*18000/20000=1800; equal/higher current price yields 0.
    order = service.get_order("O1001")
    line = service.get_line("O1001", "OI00001")
    if change == "protected":
        order = order.model_copy(update={"price_protected": True})
    elif change in {"no_drop", "higher_price"}:
        line = Line(
            line.item,
            line.sku.model_copy(update={"price_cents": 10000 if change == "no_drop" else 11000}),
        )
    elif change == "completed":
        order = order.model_copy(update={"status": "completed"})
    else:
        order = order.model_copy(update={"status": "pending_payment", "paid_at": None})
    assert "P04" in rule_ids(check_price_protection(order, line, FIXED_NOW))


def test_price_protection_one_microsecond_outside_boundary(service: Services) -> None:
    # Delta remains 1800; paid+7d+1us fails the inclusive time boundary.
    order = service.get_order("O1001").model_copy(
        update={"paid_at": FIXED_NOW - timedelta(days=7, microseconds=1)}
    )
    assert "P04" in rule_ids(
        check_price_protection(order, service.get_line("O1001", "OI00001"), FIXED_NOW)
    )


@pytest.mark.parametrize(
    "category,allowed",
    [
        ("digital", True),
        ("apparel", True),
        ("home", True),
        ("beauty", True),
        ("fresh", False),
        ("custom", False),
    ],
)
def test_return_category_rules(service: Services, category: str, allowed: bool) -> None:
    # One of two units refunds 18000*1/2=9000, below the approval threshold.
    line = service.get_line("O1001", "OI00001")
    line = Line(line.item, line.sku.model_copy(update={"category": category}))
    assert check_return(service.get_order("O1001"), [(line, 1)], FIXED_NOW).allowed == allowed


@pytest.mark.parametrize(
    "elapsed,allowed", [(timedelta(days=7), True), (timedelta(days=7, microseconds=1), False)]
)
def test_return_window_inclusive(service: Services, elapsed: timedelta, allowed: bool) -> None:
    # Refund=18000*1/2=9000; only delivered+7d time boundary changes.
    order = service.get_order("O1001").model_copy(update={"delivered_at": FIXED_NOW - elapsed})
    assert (
        check_return(order, [(service.get_line("O1001", "OI00001"), 1)], FIXED_NOW).allowed
        == allowed
    )


def test_unsigned_return_requires_handoff_and_invalid_quantities(service: Services) -> None:
    # Refund for one unit would be 9000, but unsigned status forbids the action.
    line = service.get_line("O1001", "OI00001")
    order = service.get_order("O1001").model_copy(
        update={"status": "shipped", "delivered_at": None}
    )
    assert "P14" in rule_ids(check_return(order, [(line, 1)], FIXED_NOW))
    for qty in (0, -1, 3, True):
        assert "P06" in rule_ids(check_return(service.get_order("O1001"), [(line, qty)], FIXED_NOW))
    assert "P06" in rule_ids(check_return(service.get_order("O1001"), [], FIXED_NOW))
    assert "P06" in rule_ids(
        check_return(service.get_order("O1001"), [(line, 1), (line, 1)], FIXED_NOW)
    )


@pytest.mark.parametrize(
    "amount,required", [(0, False), (19999, False), (20000, False), (20001, True)]
)
def test_approval_strict_threshold(amount: int, required: bool) -> None:
    # Approval compares the exact integer: 0,19999,20000 <=20000; 20001>20000.
    result = check_approval(amount)
    assert result.needs_approval == required and result.allowed != required


@pytest.mark.parametrize(
    "price,expected,top_up", [(8000, 3600, False), (10000, 0, False), (12000, 4000, True)]
)
def test_exchange_money_formulas(
    service: Services, price: int, expected: int, top_up: bool
) -> None:
    # Cheaper: (10000-8000)*2*18000/20000=3600; equal ->0;
    # dearer: (12000-10000)*2=4000, with no coupon scaling on the top-up.
    assert exchange_amount(service.get_line("O1001", "OI00001").item, price) == (expected, top_up)


@pytest.mark.parametrize(
    "change", ["other_spu", "same_sku", "no_stock", "returned", "exchanged", "fresh"]
)
def test_exchange_rules_reject_invalid_target_or_line(service: Services, change: str) -> None:
    # Valid target refunds 3600; the following structural/policy changes must reject it.
    line = service.get_line("O1001", "OI00001")
    target = service.get_sku("S0002")
    if change == "other_spu":
        target = service.get_sku("S0004")
    elif change == "same_sku":
        target = line.sku
    elif change == "no_stock":
        target = target.model_copy(update={"stock": 1})
    elif change == "returned":
        line = Line(line.item.model_copy(update={"returned_qty": 2}), line.sku)
    elif change == "exchanged":
        line = Line(line.item.model_copy(update={"status": "exchanged"}), line.sku)
    else:
        line = Line(line.item, line.sku.model_copy(update={"category": "fresh"}))
    assert not check_exchange(service.get_order("O1001"), line, target, FIXED_NOW).allowed


def test_exchange_exact_stock_and_shared_return_window(service: Services) -> None:
    # 2 units need exactly stock 2; refund 3600. At delivered+7d allowed, +1us rejected.
    order = service.get_order("O1001").model_copy(
        update={"delivered_at": FIXED_NOW - timedelta(days=7)}
    )
    line = service.get_line("O1001", "OI00001")
    target = service.get_sku("S0002").model_copy(update={"stock": 2})
    assert check_exchange(order, line, target, FIXED_NOW).allowed
    assert not check_exchange(order, line, target, FIXED_NOW + timedelta(microseconds=1)).allowed


def test_p15_rejects_other_users_and_same_users_other_account(service: Services) -> None:
    # Destination checks do not alter the refund amount; only PM0001 is original.
    order = service.get_order("O1001")
    for payment_id, allowed in [("PM0001", True), ("PM0002", False), ("PM0003", False)]:
        payment = service._row("payment_methods", payment_id, PaymentMethod)
        assert check_refund_destination(order, payment).allowed == allowed


def test_price_protection_persists_exact_amount_and_is_once_per_order(service: Services) -> None:
    # (10000-9000)*2*18000/(10000*2)=1800, leaving 18000-1800=16200 refundable.
    result = service.apply_price_protection("O1001", "OI00001")
    assert result.amount_cents == 1800
    assert service.get_order("O1001").price_protected
    assert service.get_line("O1001", "OI00001").item.refunded_cents == 1800
    refund = service.db.execute("SELECT * FROM refunds").fetchone()
    assert refund["amount_cents"] == 1800 and refund["payment_method_id"] == "PM0001"
    assert refund["created_at"] == FIXED_NOW.isoformat()
    assert tuple(service.db.execute("SELECT id, type, qty FROM aftersales").fetchone()) == (
        "AS0001",
        "price_protection",
        2,
    )
    previous = dump_hashes(service.db)
    with pytest.raises(PolicyError) as error:
        service.apply_price_protection("O1001", "OI00001")
    assert "P04" in error.value.rule_ids and dump_hashes(service.db) == previous


def test_partial_returns_and_prior_price_refund_conserve_cents(service: Services) -> None:
    # Price refund=1800; first unit=18000/2=9000; last unit=18000-1800-9000=7200.
    # Total refunds=1800+9000+7200=18000, exactly the original paid share.
    service.apply_price_protection("O1001", "OI00001")
    first = service.request_return("O1001", [ReturnItem(order_item_id="OI00001", qty=1)], "无理由")
    last = service.request_return("O1001", [ReturnItem(order_item_id="OI00001", qty=1)], "剩余商品")
    assert (first.amount_cents, last.amount_cents) == (9000, 7200)
    line = service.get_line("O1001", "OI00001").item
    assert (line.status, line.returned_qty, line.refunded_cents) == ("returned", 2, 18000)
    assert service.db.execute("SELECT sum(amount_cents) FROM refunds").fetchone()[0] == 18000
    previous = dump_hashes(service.db)
    with pytest.raises(PolicyError):
        service.request_return("O1001", [ReturnItem(order_item_id="OI00001", qty=1)], "重复")
    assert dump_hashes(service.db) == previous


def test_completed_order_can_return_all_and_reaches_exact_share(service: Services) -> None:
    # Returning both remaining units directly refunds 18000; stock is not replenished by v1 return.
    service.db.execute("UPDATE orders SET status='completed'")
    result = service.request_return("O1001", [ReturnItem(order_item_id="OI00001", qty=2)], "无理由")
    assert result.amount_cents == 18000
    assert service.get_sku("S0001").stock == 5


@pytest.mark.parametrize(
    "sku_id,amount,status,refund_count",
    [
        ("S0002", 3600, "completed", 1),
        ("S0003", 4000, "awaiting_payment", 0),
    ],
)
def test_exchange_updates_target_stock_and_financial_records(
    service: Services,
    sku_id: str,
    amount: int,
    status: str,
    refund_count: int,
) -> None:
    # Cheaper refund: 2000*2*18000/20000=3600. Dearer top-up: 2000*2=4000.
    # Target stock=5-2=3; old stock remains 5 because returned goods are in transit.
    result = service.request_exchange("O1001", "OI00001", sku_id, "换颜色")
    assert (result.amount_cents, result.status) == (amount, status)
    assert service.get_sku(sku_id).stock == 3 and service.get_sku("S0001").stock == 5
    assert service.db.execute("SELECT count(*) FROM refunds").fetchone()[0] == refund_count
    line = service.get_line("O1001", "OI00001").item
    assert line.status == "exchanged" and line.sku_id == "S0001"
    assert line.refunded_cents == (3600 if refund_count else 0)


def test_cheaper_exchange_then_return_only_refunds_remaining_cash(service: Services) -> None:
    # Exchange refund=3600; remaining 18000-3600=14400. Full return =>14400; cumulative=18000.
    service.request_exchange("O1001", "OI00001", "S0002", "颜色")
    result = service.request_return("O1001", [ReturnItem(order_item_id="OI00001", qty=2)], "退货")
    assert result.amount_cents == 14400
    assert service.get_line("O1001", "OI00001").item.refunded_cents == 18000


@pytest.mark.parametrize(
    "expiration,expected",
    [
        (FIXED_NOW, "unused"),
        (FIXED_NOW - timedelta(microseconds=1), "expired"),
    ],
)
def test_cancel_restocks_and_restores_coupon_at_inclusive_expiry(
    service: Services,
    expiration: datetime,
    expected: str,
) -> None:
    # Paid=2*10000-2000=18000, cancellation refund=18000; old stock=5+2=7.
    service.db.execute(
        "UPDATE orders SET status='pending_shipment', shipped_at=NULL, delivered_at=NULL"
    )
    service.db.execute("UPDATE coupons SET valid_to=?", (expiration.isoformat(),))
    result = service.cancel_order("O1001", "不需要")
    assert result.amount_cents == 18000 and service.get_order("O1001").status == "cancelled"
    assert service.get_sku("S0001").stock == 7
    assert tuple(
        service.db.execute(
            "SELECT status, used_order_id FROM user_coupons WHERE id='UC0001'"
        ).fetchone()
    ) == (expected, None)
    assert service.db.execute("SELECT aftersale_id FROM refunds").fetchone()[0] is None


def test_cancel_after_price_protection_only_refunds_outstanding_balance(service: Services) -> None:
    # Price refund=1800. Cancellation refunds 18000-1800=16200; combined exactly 18000.
    service.db.execute(
        "UPDATE orders SET status='pending_shipment', shipped_at=NULL, delivered_at=NULL"
    )
    service.apply_price_protection("O1001", "OI00001")
    assert service.cancel_order("O1001", "取消").amount_cents == 16200
    assert service.db.execute("SELECT sum(amount_cents) FROM refunds").fetchone()[0] == 18000
    assert service.get_line("O1001", "OI00001").item.refunded_cents == 18000


def test_unpaid_cancel_has_no_refund(service: Services) -> None:
    # Unpaid order: paid cents=0, so cancellation creates no refund; reserved stock 5+2=7.
    service.db.execute("""UPDATE orders SET status='pending_payment', paid_at=NULL,
        shipped_at=NULL, delivered_at=NULL, paid_cents=0""")
    result = service.cancel_order("O1001", "取消")
    assert result.amount_cents == 0 and result.record_ids == []
    assert service.db.execute("SELECT count(*) FROM refunds").fetchone()[0] == 0
    assert service.get_line("O1001", "OI00001").item.refunded_cents == 0


def test_cancel_above_approval_threshold_is_exempt(service: Services) -> None:
    # Gross=2*20000=40000, discount=2000, paid=38000; cancellation refunds all 38000 (>20000) without approval.
    service.db.execute("""UPDATE orders SET status='pending_shipment', shipped_at=NULL,
        delivered_at=NULL, items_total_cents=40000, paid_cents=38000""")
    service.db.execute("UPDATE order_items SET unit_price_cents=20000, paid_share_cents=38000")
    assert service.cancel_order("O1001", "取消").amount_cents == 38000
    assert service.db.execute("SELECT count(*) FROM approvals").fetchone()[0] == 0


def test_modify_address_success_and_shipped_rejection_are_atomic(service: Services) -> None:
    # Money remains 18000 throughout: 2*10000-2000, address updates have no financial effect.
    service.db.execute("UPDATE orders SET status='pending_shipment'")
    replacement = ADDRESS.model_copy(update={"detail": "新地址2号"})
    service.modify_address("O1001", replacement)
    assert service.get_order("O1001").address == replacement
    service.db.execute("UPDATE orders SET status='shipped'")
    previous = dump_hashes(service.db)
    with pytest.raises(PolicyError):
        service.modify_address("O1001", ADDRESS)
    assert dump_hashes(service.db) == previous


def test_create_order_coupon_allocation_stock_and_ids(service: Services) -> None:
    # Current prices: 9000*2=18000 and 8000*1=8000, gross=26000.
    # Discount2000: 1384+16000/26000, 615+10000/26000; floors1999,
    # extra cent to line1 =>1385/615; paid shares16615/7385, total24000.
    result = service.create_order(
        "U001",
        [PurchaseItem(sku_id="S0001", qty=2), PurchaseItem(sku_id="S0002", qty=1)],
        "PM0001",
        "UC0002",
    )
    assert result.order_id == "O1002" and result.amount_cents == 24000
    order = service.get_order(result.order_id)
    assert order.status == "pending_shipment" and order.paid_at == FIXED_NOW
    assert order.address == ADDRESS
    rows = service.db.execute(
        "SELECT id, paid_share_cents FROM order_items WHERE order_id=? ORDER BY id",
        (result.order_id,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [("OI00002", 16615), ("OI00003", 7385)]
    assert (service.get_sku("S0001").stock, service.get_sku("S0002").stock) == (3, 4)
    assert tuple(
        service.db.execute(
            "SELECT status, used_order_id FROM user_coupons WHERE id='UC0002'"
        ).fetchone()
    ) == ("used", "O1002")


@pytest.mark.parametrize("boundary", ["from", "to"])
def test_create_at_coupon_validity_boundary(service: Services, boundary: str) -> None:
    # 9000*2=18000, coupon2000 ->16000; both validity endpoints are inclusive.
    column = "valid_from" if boundary == "from" else "valid_to"
    service.db.execute(f"UPDATE coupons SET {column}=?", (FIXED_NOW.isoformat(),))
    assert (
        service.create_order(
            "U001", [PurchaseItem(sku_id="S0001", qty=2)], "PM0001", "UC0002"
        ).amount_cents
        == 16000
    )


@pytest.mark.parametrize(
    "change",
    [
        "expired",
        "future",
        "used",
        "foreign",
        "threshold",
        "discount",
        "stock",
        "duplicate",
        "empty",
        "two_coupons",
    ],
)
def test_create_invalid_inputs_leave_every_table_unchanged(service: Services, change: str) -> None:
    # Default gross9000*2=18000, discount2000 ->16000; failures must not debit stock or consume a coupon.
    requests = [PurchaseItem(sku_id="S0001", qty=2)]
    coupon_id = "UC0002"
    if change == "expired":
        service.db.execute(
            "UPDATE coupons SET valid_to=?", ((FIXED_NOW - timedelta(microseconds=1)).isoformat(),)
        )
    elif change == "future":
        service.db.execute(
            "UPDATE coupons SET valid_from=?",
            ((FIXED_NOW + timedelta(microseconds=1)).isoformat(),),
        )
    elif change == "used":
        coupon_id = "UC0001"
    elif change == "foreign":
        service.db.execute("UPDATE user_coupons SET user_id='U002' WHERE id='UC0002'")
    elif change == "threshold":
        requests = [PurchaseItem(sku_id="S0001", qty=1)]  # 9000<10000 threshold.
    elif change == "discount":
        service.db.execute("UPDATE coupons SET discount_cents=18001")  # 18001>18000 gross.
    elif change == "stock":
        requests = [PurchaseItem(sku_id="S0001", qty=6)]  # 6>5 stock.
    elif change == "duplicate":
        requests *= 2
    elif change == "empty":
        requests = []
    else:
        coupon_id = ["UC0001", "UC0002"]
    previous = dump_hashes(service.db)
    with pytest.raises((PolicyError, ValueError)):
        service.create_order("U001", requests, "PM0001", coupon_id)
    assert dump_hashes(service.db) == previous


@pytest.mark.parametrize("action", ["price", "return", "exchange"])
@pytest.mark.parametrize("amount", [20000, 20001])
def test_refund_actions_enforce_approval_boundary_before_any_write(
    service: Services,
    action: str,
    amount: int,
) -> None:
    # No coupon, one unit: return paid=amount; price/exchange old=amount+1,new=1 =>amount refund.
    # Exactly20000 is allowed; 20001 must leave all 14 tables unchanged.
    unit = amount if action == "return" else amount + 1
    service.db.execute(
        "UPDATE orders SET items_total_cents=?,discount_cents=0,paid_cents=?", (unit, unit)
    )
    service.db.execute(
        "UPDATE order_items SET qty=1,unit_price_cents=?,paid_share_cents=?", (unit, unit)
    )
    service.db.execute("UPDATE skus SET price_cents=1 WHERE id IN ('S0001','S0002')")
    previous = dump_hashes(service.db)

    def execute() -> int:
        if action == "price":
            return service.apply_price_protection("O1001", "OI00001").amount_cents
        if action == "return":
            return service.request_return(
                "O1001", [ReturnItem(order_item_id="OI00001", qty=1)], "退货"
            ).amount_cents
        return service.request_exchange("O1001", "OI00001", "S0002", "换货").amount_cents

    if amount == 20000:
        assert execute() == amount
    else:
        with pytest.raises(PolicyError) as error:
            execute()
        assert "P08" in error.value.rule_ids and dump_hashes(service.db) == previous
        request = ApprovalRequest(
            action={
                "price": "apply_price_protection",
                "return": "request_return",
                "exchange": "request_exchange",
            }[action],
            args={"order_item_id": "OI00001"},
        )
        approval = service.submit_approval("O1001", request, "金额超过阈值")
        assert approval.status == "pending" and approval.record_ids == ["AP0001"]
        assert service.db.execute("SELECT count(*) FROM refunds").fetchone()[0] == 0


@pytest.mark.parametrize("second_share", [10000, 10001])
def test_multiline_return_approval_uses_request_sum(service: Services, second_share: int) -> None:
    # Two lines gross12000+12000=24000. Paid10000+10000=20000 allowed,
    # or10000+10001=20001 denied although each individual line is below20000.
    paid = 10000 + second_share
    service.db.execute(
        "UPDATE orders SET items_total_cents=24000,discount_cents=?,paid_cents=?",
        (24000 - paid, paid),
    )
    service.db.execute("UPDATE order_items SET qty=1,unit_price_cents=12000,paid_share_cents=10000")
    insert(
        service.db,
        "order_items",
        id="OI00002",
        order_id="O1001",
        sku_id="S0002",
        qty=1,
        unit_price_cents=12000,
        paid_share_cents=second_share,
        status="normal",
        returned_qty=0,
        refunded_cents=0,
    )
    requests = [
        ReturnItem(order_item_id="OI00001", qty=1),
        ReturnItem(order_item_id="OI00002", qty=1),
    ]
    previous = dump_hashes(service.db)
    if paid == 20000:
        assert service.request_return("O1001", requests, "一起退").amount_cents == 20000
    else:
        with pytest.raises(PolicyError) as error:
            service.request_return("O1001", requests, "一起退")
        assert "P08" in error.value.rule_ids and dump_hashes(service.db) == previous


@pytest.mark.parametrize("destination", ["PM0002", "PM0003", "nonexistent", ""])
@pytest.mark.parametrize("action", ["price", "return", "exchange", "cancel"])
def test_p15_cannot_redirect_any_refund(service: Services, destination: str, action: str) -> None:
    # Valid amounts would be price1800, return18000, exchange3600, cancel18000.
    # A foreign user's account, own different account, unknown ID and empty ID all fail P15 before writes.
    service.db.execute(
        "UPDATE orders SET status='pending_shipment'" if action == "cancel" else "SELECT 1"
    )
    previous = dump_hashes(service.db)
    with pytest.raises(PolicyError) as error:
        if action == "price":
            service.apply_price_protection("O1001", "OI00001", refund_payment_method_id=destination)
        elif action == "return":
            service.request_return(
                "O1001",
                [ReturnItem(order_item_id="OI00001", qty=2)],
                "退货",
                refund_payment_method_id=destination,
            )
        elif action == "exchange":
            service.request_exchange(
                "O1001", "OI00001", "S0002", "换货", refund_payment_method_id=destination
            )
        else:
            service.cancel_order("O1001", "取消", refund_payment_method_id=destination)
    assert "P15" in error.value.rule_ids and dump_hashes(service.db) == previous


def test_payment_failure_rolls_back_aftersale_and_money_updates(service: Services) -> None:
    # Expected refund1800=(10000-9000)*2*18000/20000. Simulated refund failure rolls back the entire action.
    service.db.execute("""CREATE TRIGGER fail_refund BEFORE INSERT ON refunds BEGIN
        SELECT RAISE(ABORT, 'simulated payment failure'); END""")
    previous = dump_hashes(service.db)
    with pytest.raises(sqlite3.IntegrityError):
        service.apply_price_protection("O1001", "OI00001")
    assert dump_hashes(service.db) == previous


def test_late_create_failure_rolls_back_order_lines_stock_and_coupon(service: Services) -> None:
    # Gross9000+8000=17000, coupon2000, paid15000. Even failure on the second line
    # must undo the first line's stock decrement5->4 and the new order.
    service.db.execute("""CREATE TRIGGER fail_line BEFORE INSERT ON order_items
        WHEN NEW.sku_id='S0002' BEGIN SELECT RAISE(ABORT, 'simulated line failure'); END""")
    previous = dump_hashes(service.db)
    with pytest.raises(sqlite3.IntegrityError):
        service.create_order(
            "U001",
            [PurchaseItem(sku_id="S0001", qty=1), PurchaseItem(sku_id="S0002", qty=1)],
            "PM0001",
            "UC0002",
        )
    assert dump_hashes(service.db) == previous


def test_equal_price_exchange_and_no_coupon_exact_stock_order(service: Services) -> None:
    # Equal exchange price10000 yields0 refund/top-up and consumes two units; no refund row.
    service.db.execute("UPDATE skus SET price_cents=10000 WHERE id='S0002'")
    assert service.request_exchange("O1001", "OI00001", "S0002", "等价").amount_cents == 0
    assert service.db.execute("SELECT count(*) FROM refunds").fetchone()[0] == 0
    # New order currentprice9000*stock5=45000, no coupon =>45000; stock5-5=0.
    result = service.create_order(
        "U001",
        [PurchaseItem(sku_id="S0001", qty=5)],
        "PM0001",
        address=ADDRESS.model_copy(update={"detail": "新地址"}),
    )
    assert result.amount_cents == 45000 and service.get_sku("S0001").stock == 0
    assert service.get_order(result.order_id).address.detail == "新地址"


def test_coupon_threshold_and_maximum_count_pure_checks(service: Services) -> None:
    # At threshold10000, discount2000 gives8000; two copies of the coupon are forbidden by P13.
    sku = service.get_sku("S0001").model_copy(update={"price_cents": 10000})
    payment = service._row("payment_methods", "PM0001", PaymentMethod)
    owned = service._row("user_coupons", "UC0002", UserCoupon)
    coupon = service._row("coupons", "C01", Coupon)
    result = check_create_order("U001", [(sku, 1)], payment, [(owned, coupon)], FIXED_NOW)
    assert result.allowed and result.amount_cents == 8000
    assert "P13" in rule_ids(
        check_create_order("U001", [(sku, 1)], payment, [(owned, coupon)] * 2, FIXED_NOW)
    )


@pytest.mark.parametrize("case", ["late_address", "fresh_return", "expired_coupon", "approval"])
def test_policy_ablation_is_explicit_and_default_is_enforced(service: Services, case: str) -> None:
    # Address: money unchanged18000. Fresh return: one unit=18000/2=9000.
    # Expired coupon order: gross9000*2=18000, discount2000 ->16000.
    # Large return: gross=paid=20001, no discount, one unit ->20001 (>20000).
    if case == "fresh_return":
        service.db.execute("UPDATE spus SET category='fresh' WHERE id='SPU01'")
    elif case == "expired_coupon":
        service.db.execute(
            "UPDATE coupons SET valid_to=?", ((FIXED_NOW - timedelta(days=1)).isoformat(),)
        )
    elif case == "approval":
        service.db.execute(
            "UPDATE orders SET items_total_cents=20001,discount_cents=0,paid_cents=20001"
        )
        service.db.execute(
            "UPDATE order_items SET qty=1,unit_price_cents=20001,paid_share_cents=20001"
        )

    def execute() -> None:
        if case == "late_address":
            service.modify_address("O1001", ADDRESS.model_copy(update={"detail": "变化地址"}))
        elif case == "expired_coupon":
            service.create_order("U001", [PurchaseItem(sku_id="S0001", qty=2)], "PM0001", "UC0002")
        else:
            service.request_return("O1001", [ReturnItem(order_item_id="OI00001", qty=1)], "退货")

    with pytest.raises(PolicyError):
        execute()
    service = Services(service.db, service.clock, enforce_policy=False)
    execute()


@pytest.mark.parametrize("action", ["price", "return", "exchange", "cancel"])
def test_p15_survives_policy_ablation(service: Services, action: str) -> None:
    # Policy switch never changes destination: amounts1800/18000/3600/18000 must all return via PM0001.
    service = Services(service.db, service.clock, enforce_policy=False)
    previous = dump_hashes(service.db)
    with pytest.raises(PolicyError) as error:
        if action == "price":
            service.apply_price_protection("O1001", "OI00001", refund_payment_method_id="PM0002")
        elif action == "return":
            service.request_return(
                "O1001",
                [ReturnItem(order_item_id="OI00001", qty=2)],
                "退货",
                refund_payment_method_id="PM0002",
            )
        elif action == "exchange":
            service.request_exchange(
                "O1001", "OI00001", "S0002", "换货", refund_payment_method_id="PM0002"
            )
        else:
            service.cancel_order("O1001", "取消", refund_payment_method_id="PM0002")
    assert error.value.rule_ids == ["P15"] and dump_hashes(service.db) == previous


def test_data_integrity_survives_policy_ablation(service: Services) -> None:
    # Invalid duplicate return would request9000+9000 from the same unit twice, so it stays blocked.
    service = Services(service.db, service.clock, enforce_policy=False)
    previous = dump_hashes(service.db)
    with pytest.raises(PolicyError):
        service.request_return("O1001", [ReturnItem(order_item_id="OI00001", qty=1)] * 2, "重复行")
    assert dump_hashes(service.db) == previous
    # Cancellation refunds18000 once; second call would duplicate restocking5+2+2, so it stays blocked.
    service.cancel_order("O1001", "消融取消")
    previous = dump_hashes(service.db)
    with pytest.raises(ValueError):
        service.cancel_order("O1001", "重复取消")
    assert dump_hashes(service.db) == previous


def test_price_difference_rounds_half_up_and_caps_prior_refunds() -> None:
    # Original unit200, qty1, paid100; newprice199 =>(200-199)*1*100/200=0.5 ->1.
    # With all100 already refunded, remaining cash0, so the same calculation must be capped at0.
    line = item(qty=1, unit_price_cents=200, paid_share_cents=100)
    assert proportional_difference(line, 199) == 1
    assert proportional_difference(line.model_copy(update={"refunded_cents": 100}), 199) == 0


@pytest.mark.parametrize("numerator,denominator", [(-1, 2), (1, 0), (1, -1), (True, 2), (1, True)])
def test_rounding_rejects_invalid_rationals(numerator: int, denominator: int) -> None:
    # A refund rational requires integer numerator>=0 and denominator>0; bool is not money.
    with pytest.raises(ValueError):
        round_half_up(numerator, denominator)


def test_full_coupon_zero_payment_and_zero_refund(service: Services) -> None:
    # Coupon18000 exactly matches gross9000*2=18000: paid0, shares0, zero cash refund on cancellation.
    service.db.execute("UPDATE coupons SET discount_cents=18000")
    result = service.create_order("U001", [PurchaseItem(sku_id="S0001", qty=2)], "PM0001", "UC0002")
    assert result.amount_cents == 0 and service.get_order(result.order_id).paid_cents == 0
    assert service.cancel_order(result.order_id, "取消免费订单").amount_cents == 0
    assert service.db.execute("SELECT amount_cents FROM refunds").fetchone()[0] == 0


def test_partial_return_then_exchange_uses_only_remaining_units(service: Services) -> None:
    # First of two units refunds18000/2=9000. Exchange remaining one:
    # (10000-8000)*1*18000/20000=1800; cumulative10800; new stock5-1=4.
    service.request_return("O1001", [ReturnItem(order_item_id="OI00001", qty=1)], "部分退货")
    result = service.request_exchange("O1001", "OI00001", "S0002", "剩余换色")
    assert result.amount_cents == 1800 and service.get_sku("S0002").stock == 4
    assert service.get_line("O1001", "OI00001").item.refunded_cents == 10800


def test_foreign_order_item_and_payment_cannot_be_used(service: Services) -> None:
    # IDs with no matching relationship must not produce the otherwise expected1800 price refund.
    previous = dump_hashes(service.db)
    with pytest.raises(ValueError):
        service.apply_price_protection("O9999", "OI00001")
    with pytest.raises(ValueError):
        service.get_line("O1002", "OI00001")
    with pytest.raises(PolicyError) as error:
        service.create_order("U001", [PurchaseItem(sku_id="S0001", qty=1)], "PM0002")
    assert error.value.rule_ids == ["P15"] and dump_hashes(service.db) == previous

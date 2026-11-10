"""Integer money arithmetic and shared, side-effect-free policy checks."""

import json
import sqlite3
from datetime import datetime, timedelta
from typing import TypeVar

from pydantic import BaseModel, JsonValue

from aftersales.domain.clock import Clock
from aftersales.domain.db import insert, next_id, transaction
from aftersales.domain.models import (
    SKU,
    Address,
    ApprovalRequest,
    AuthResult,
    BusinessSnapshot,
    CheckResult,
    Coupon,
    CouponDetails,
    Line,
    OperationResult,
    Order,
    OrderDetails,
    OrderItem,
    OrderStatus,
    PaymentMethod,
    PricePoint,
    Product,
    PurchaseItem,
    RefundAction,
    RefundPreview,
    ReturnItem,
    Shipment,
    User,
    UserCoupon,
    UserDetails,
    Violation,
)

WINDOW = timedelta(days=7)
APPROVAL_LIMIT_CENTS = 20_000


def format_cents(amount: int) -> str:
    _integer(amount)
    return f"¥{amount // 100}.{amount % 100:02d}"


class PolicyError(ValueError):
    def __init__(self, result: CheckResult) -> None:
        self.result = result
        self.rule_ids = [violation.rule_id for violation in result.violations]
        super().__init__(
            "; ".join(
                f"[{violation.rule_id}] {violation.reason}" for violation in result.violations
            )
        )


def _integer(value: int, *, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError("Expected an integer within range")


def round_half_up(numerator: int, denominator: int) -> int:
    """Round a nonnegative rational to the nearest integer, with ties upward."""
    _integer(numerator)
    _integer(denominator, minimum=1)
    return (2 * numerator + denominator) // (2 * denominator)


def allocate_discount(line_totals: list[int], discount_cents: int) -> list[int]:
    """Allocate cents using largest remainders, with input order breaking ties."""
    _integer(discount_cents)
    if not line_totals:
        raise ValueError("At least one line is required")
    for total in line_totals:
        _integer(total, minimum=1)
    total_cents = sum(line_totals)
    if discount_cents > total_cents:
        raise ValueError("Discount exceeds order total")
    portions = [divmod(discount_cents * line, total_cents) for line in line_totals]
    allocated = [quotient for quotient, remainder in portions]
    ranking = sorted(range(len(portions)), key=lambda index: (-portions[index][1], index))
    for index in ranking[: discount_cents - sum(allocated)]:
        allocated[index] += 1
    return allocated


def paid_shares(line_totals: list[int], discount_cents: int) -> list[int]:
    return [
        total - discount
        for total, discount in zip(
            line_totals, allocate_discount(line_totals, discount_cents), strict=True
        )
    ]


def partial_refund(item: OrderItem, qty: int) -> int:
    _integer(qty, minimum=1)
    remaining_qty = item.qty - item.returned_qty
    if qty > remaining_qty:
        raise ValueError("Return quantity exceeds remaining quantity")
    remaining_paid = item.paid_share_cents - item.refunded_cents
    if qty == remaining_qty:
        return remaining_paid
    return min(round_half_up(item.paid_share_cents * qty, item.qty), remaining_paid)


def proportional_difference(item: OrderItem, new_price_cents: int, qty: int | None = None) -> int:
    _integer(new_price_cents, minimum=1)
    quantity = item.qty if qty is None else qty
    _integer(quantity, minimum=1)
    difference = max(item.unit_price_cents - new_price_cents, 0)
    calculated = round_half_up(
        difference * quantity * item.paid_share_cents, item.unit_price_cents * item.qty
    )
    return min(calculated, item.paid_share_cents - item.refunded_cents)


def _issue(rule_id: str, reason: str) -> Violation:
    return Violation(rule_id=rule_id, reason=reason)


def check_approval(amount_cents: int) -> CheckResult:
    _integer(amount_cents)
    required = amount_cents > APPROVAL_LIMIT_CENTS
    return CheckResult(
        amount_cents=amount_cents,
        needs_approval=required,
        violations=[_issue("P08", "单笔售后退款超过 ¥200，必须提交人工审批")] if required else [],
    )


def check_refund_destination(order: Order, payment: PaymentMethod) -> CheckResult:
    violations = []
    if payment.id != order.payment_method_id or payment.user_id != order.user_id:
        violations.append(_issue("P15", "退款只能退回该订单原支付账户"))
    return CheckResult(violations=violations)


def check_modify_address(order: Order) -> CheckResult:
    return CheckResult(
        violations=[]
        if order.status in {"pending_payment", "pending_shipment"}
        else [_issue("P03", "只有待支付、待发货订单可以修改地址")]
    )


def check_cancel_order(order: Order, already_refunded_cents: int = 0) -> CheckResult:
    _integer(already_refunded_cents)
    if already_refunded_cents > order.paid_cents:
        raise ValueError("Refunded amount exceeds order payment")
    return CheckResult(
        amount_cents=order.paid_cents - already_refunded_cents,
        violations=[]
        if order.status in {"pending_payment", "pending_shipment"}
        else [_issue("P03", "只有待支付、待发货订单可以取消")],
    )


def check_price_protection(order: Order, line: Line, now: datetime) -> CheckResult:
    violations = []
    if order.status not in {"pending_shipment", "shipped", "delivered"}:
        violations.append(_issue("P04", "订单状态不支持价保"))
    if order.paid_at is None or (order.delivered_at is not None and now > order.paid_at + WINDOW):
        violations.append(_issue("P04", "已签收订单超过支付后 7 天价保期"))
    if order.price_protected:
        violations.append(_issue("P04", "每单只能价保一次"))
    if line.sku.price_cents >= line.item.unit_price_cents:
        violations.append(_issue("P04", "当前价格未低于下单价格"))
    if line.item.returned_qty == line.item.qty:
        violations.append(_issue("P04", "该商品已全部退货"))
    amount = proportional_difference(line.item, line.sku.price_cents)
    approval = check_approval(amount)
    return CheckResult(
        violations=violations + approval.violations,
        amount_cents=amount,
        needs_approval=approval.needs_approval,
    )


def _return_window(order: Order, line: Line, now: datetime) -> list[Violation]:
    violations = []
    if order.delivered_at is None:
        violations.append(_issue("P14", "未签收不能退货，拒收请转人工"))
    if order.status not in {"delivered", "completed"}:
        violations.append(_issue("P05", "只有已签收或已完成订单可无理由退换"))
    if order.delivered_at is not None and now > order.delivered_at + WINDOW:
        violations.append(_issue("P05", "超过签收后 7 天退换期限"))
    if line.sku.category in {"fresh", "custom"}:
        violations.append(_issue("P05", "生鲜和定制商品不支持无理由退换"))
    return violations


def check_return(order: Order, lines: list[tuple[Line, int]], now: datetime) -> CheckResult:
    violations = []
    amount = 0
    seen: set[str] = set()
    if not lines:
        violations.append(_issue("P06", "退货清单不能为空"))
    for line, qty in lines:
        violations.extend(_return_window(order, line, now))
        if line.item.id in seen:
            violations.append(_issue("P06", "退货清单不能重复同一商品行"))
            continue
        seen.add(line.item.id)
        if type(qty) is not int or qty <= 0 or qty > line.item.qty - line.item.returned_qty:
            violations.append(_issue("P06", "退货数量必须为正整数且不超过剩余数量"))
            continue
        amount += partial_refund(line.item, qty)
    approval = check_approval(amount)
    return CheckResult(
        violations=violations + approval.violations,
        amount_cents=amount,
        needs_approval=approval.needs_approval,
    )


def exchange_amount(item: OrderItem, new_price_cents: int) -> tuple[int, bool]:
    """Return amount and whether it is a top-up, exchanging the unreturned units."""
    _integer(new_price_cents, minimum=1)
    quantity = item.qty - item.returned_qty
    if new_price_cents > item.unit_price_cents:
        return (new_price_cents - item.unit_price_cents) * quantity, True
    return proportional_difference(item, new_price_cents, quantity), False


def check_exchange(order: Order, line: Line, new_sku: SKU, now: datetime) -> CheckResult:
    violations = _return_window(order, line, now)
    quantity = line.item.qty - line.item.returned_qty
    if quantity <= 0:
        violations.append(_issue("P09", "该商品行已全部退货"))
    elif line.item.status == "exchanged":
        violations.append(_issue("P09", "同一商品行不重复换货，请转人工"))
    if new_sku.id == line.item.sku_id or new_sku.spu_id != line.sku.spu_id:
        violations.append(_issue("P09", "只能换成同一 SPU 的其他 SKU"))
    if new_sku.stock < quantity:
        violations.append(_issue("P09", "换货目标库存不足"))
    amount, top_up = exchange_amount(line.item, new_sku.price_cents) if quantity > 0 else (0, False)
    approval = check_approval(0 if top_up else amount)
    return CheckResult(
        violations=violations + approval.violations,
        amount_cents=amount,
        needs_approval=approval.needs_approval,
    )


def check_create_order(
    user_id: str,
    items: list[tuple[SKU, int]],
    payment: PaymentMethod,
    coupons: list[tuple[UserCoupon, Coupon]],
    now: datetime,
) -> CheckResult:
    violations = []
    total = 0
    seen: set[str] = set()
    if payment.user_id != user_id:
        violations.append(_issue("P15", "只能使用本人的支付方式"))
    if not items:
        violations.append(_issue("DATA", "下单清单不能为空"))
    for sku, qty in items:
        if sku.id in seen:
            violations.append(_issue("DATA", "下单清单不能重复 SKU，请合并数量"))
        seen.add(sku.id)
        if type(qty) is not int or qty <= 0:
            violations.append(_issue("DATA", "购买数量必须为正整数"))
            continue
        total += sku.price_cents * qty
        if sku.stock < qty:
            violations.append(_issue("STOCK", "购买库存不足"))
    if len(coupons) > 1:
        violations.append(_issue("P13", "每单最多使用一张优惠券"))
    discount = 0
    for user_coupon, coupon in coupons:
        if user_coupon.user_id != user_id or user_coupon.status != "unused":
            violations.append(_issue("P13", "优惠券必须属于本人且未使用"))
        if not coupon.valid_from <= now <= coupon.valid_to:
            violations.append(_issue("P13", "优惠券不在有效期内"))
        if total < coupon.threshold_cents or total < coupon.discount_cents:
            violations.append(_issue("P13", "订单不满足优惠券门槛或优惠额超过总额"))
        discount += coupon.discount_cents
    return CheckResult(violations=violations, amount_cents=max(total - discount, 0))


Model = TypeVar("Model", bound=BaseModel)


class Services:
    """Own database effects; public checks above consume immutable snapshots."""

    def __init__(
        self, db: sqlite3.Connection, clock: Clock, *, enforce_policy: bool = True
    ) -> None:
        self.db = db
        self.clock = clock
        self.enforce_policy = enforce_policy

    def _row(self, table: str, record_id: str, model: type[Model]) -> Model:
        tables = {
            "orders",
            "order_items",
            "skus",
            "coupons",
            "user_coupons",
            "payment_methods",
            "users",
        }
        if table not in tables:
            raise ValueError("Unknown snapshot table")
        row = self.db.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise ValueError(f"不存在的资源：{table}/{record_id}")
        values = dict(row)
        for field in ("address", "attrs", "default_address"):
            if field in values:
                values[field] = json.loads(values[field])
        return model.model_validate(values)

    def find_user_by_phone(self, phone: str, name: str) -> AuthResult:
        row = self.db.execute(
            "SELECT id FROM users WHERE phone=? AND name=?", (phone, name)
        ).fetchone()
        return (
            AuthResult(status="authenticated", user_id=row[0])
            if row
            else AuthResult(status="not_found")
        )

    def find_user_by_order(self, order_id: str, receiver_name: str) -> AuthResult:
        row = self.db.execute(
            "SELECT user_id, address FROM orders WHERE id=?", (order_id,)
        ).fetchone()
        if row is None or json.loads(row["address"])["receiver_name"] != receiver_name:
            return AuthResult(status="not_found")
        return AuthResult(status="authenticated", user_id=row["user_id"])

    def get_user(self, user_id: str) -> UserDetails:
        user = self._row("users", user_id, User)
        payments = [
            PaymentMethod.model_validate(dict(row))
            for row in self.db.execute(
                "SELECT * FROM payment_methods WHERE user_id=? ORDER BY id", (user_id,)
            )
        ]
        return UserDetails(**user.model_dump(), payment_methods=payments)

    def list_orders(self, user_id: str, status: OrderStatus | None = None) -> list[Order]:
        return [
            self.get_order(row[0])
            for row in self.db.execute(
                "SELECT id FROM orders WHERE user_id=? AND (? IS NULL OR status=?) ORDER BY id",
                (user_id, status, status),
            )
        ]

    def order_details(self, order_id: str) -> OrderDetails:
        items = [
            self._row("order_items", row[0], OrderItem)
            for row in self.db.execute(
                "SELECT id FROM order_items WHERE order_id=? ORDER BY id", (order_id,)
            )
        ]
        row = self.db.execute("SELECT * FROM shipments WHERE order_id=?", (order_id,)).fetchone()
        return OrderDetails(
            order=self.get_order(order_id),
            items=items,
            shipment=Shipment.model_validate(dict(row)) if row else None,
        )

    def action_snapshot(
        self,
        order_id: str | None,
        *,
        sku_ids: list[str] | None = None,
        user_coupon_id: str | None = None,
    ) -> BusinessSnapshot:
        """Read affected rows inside the caller's execution transaction."""

        def rows(table: str, column: str, ids: list[str]) -> list[dict[str, JsonValue]]:
            if not ids:
                return []
            placeholders = ",".join("?" for _ in ids)
            result = []
            for row in self.db.execute(
                f"SELECT * FROM {table} WHERE {column} IN ({placeholders}) ORDER BY {column}, rowid",
                ids,
            ):
                value = dict(row)
                for field in ("address", "attrs"):
                    if field in value:
                        value[field] = json.loads(value[field])
                result.append(value)
            return result

        orders = rows("orders", "id", [order_id] if order_id else [])
        items = rows("order_items", "order_id", [order_id] if order_id else [])
        skus = sorted(set(sku_ids or []) | {str(item["sku_id"]) for item in items})
        coupons = {user_coupon_id} if user_coupon_id else set()
        coupons |= {str(order["user_coupon_id"]) for order in orders if order["user_coupon_id"]}
        return BusinessSnapshot(
            orders=orders,
            order_items=items,
            skus=rows("skus", "id", skus),
            user_coupons=rows("user_coupons", "id", sorted(coupons)),
            aftersales=rows("aftersales", "order_id", [order_id] if order_id else []),
            refunds=rows("refunds", "order_id", [order_id] if order_id else []),
            shipments=rows("shipments", "order_id", [order_id] if order_id else []),
        )

    def get_product(self, spu_id: str | None = None, sku_id: str | None = None) -> Product:
        if (spu_id is None) == (sku_id is None):
            raise ValueError("请且仅请提供 spu_id 或 sku_id")
        if sku_id is not None:
            spu_id = self.get_sku(sku_id).spu_id
        row = self.db.execute("SELECT * FROM spus WHERE id=?", (spu_id,)).fetchone()
        if row is None:
            raise ValueError("商品不存在")
        skus = [
            self.get_sku(item[0])
            for item in self.db.execute("SELECT id FROM skus WHERE spu_id=? ORDER BY id", (spu_id,))
        ]
        return Product(**dict(row), skus=skus)

    def get_price_history(self, sku_id: str) -> list[PricePoint]:
        self.get_sku(sku_id)
        return [
            PricePoint.model_validate(dict(row))
            for row in self.db.execute(
                "SELECT * FROM price_history WHERE sku_id=? ORDER BY effective_at", (sku_id,)
            )
        ]

    def list_user_coupons(self, user_id: str, status: str | None = None) -> list[CouponDetails]:
        owned = [
            UserCoupon.model_validate(dict(row))
            for row in self.db.execute(
                "SELECT * FROM user_coupons WHERE user_id=? AND (? IS NULL OR status=?) ORDER BY id",
                (user_id, status, status),
            )
        ]
        return [
            CouponDetails(user_coupon=item, coupon=self._row("coupons", item.coupon_id, Coupon))
            for item in owned
        ]

    def refunded_cents(self, order_id: str) -> int:
        return self.db.execute(
            "SELECT coalesce(sum(amount_cents),0) FROM refunds WHERE order_id=?", (order_id,)
        ).fetchone()[0]

    def check_purchase(
        self,
        user_id: str,
        items: list[PurchaseItem],
        payment_method_id: str,
        user_coupon_id: str | None = None,
    ) -> CheckResult:
        coupons = []
        if user_coupon_id:
            owned = self._row("user_coupons", user_coupon_id, UserCoupon)
            coupons = [(owned, self._row("coupons", owned.coupon_id, Coupon))]
        return check_create_order(
            user_id,
            [(self.get_sku(item.sku_id), item.qty) for item in items],
            self._row("payment_methods", payment_method_id, PaymentMethod),
            coupons,
            self.clock.now(),
        )

    def preview_refund(
        self,
        order_id: str,
        action: RefundAction,
        order_item_id: str | None = None,
        qty: int | None = None,
        new_sku_id: str | None = None,
    ) -> RefundPreview:
        order = self.get_order(order_id)
        direction = "refund"
        if action == "cancel_order":
            result = check_cancel_order(order, self.refunded_cents(order_id))
        else:
            if order_item_id is None:
                raise ValueError("售后试算需要商品行编号")
            line = self.get_line(order_id, order_item_id)
            if action == "apply_price_protection":
                result = check_price_protection(order, line, self.clock.now())
            elif action == "request_return":
                quantity = qty if qty is not None else line.item.qty - line.item.returned_qty
                result = check_return(order, [(line, quantity)], self.clock.now())
            elif action == "request_exchange" and new_sku_id is not None:
                target = self.get_sku(new_sku_id)
                result = check_exchange(order, line, target, self.clock.now())
                if target.price_cents > line.item.unit_price_cents:
                    direction = "top_up"
            else:
                raise ValueError("不支持的试算动作或缺少换货 SKU")
        return RefundPreview(
            **result.model_dump(),
            direction=direction,
            amount_display=format_cents(result.amount_cents),
        )

    def get_order(self, order_id: str) -> Order:
        return self._row("orders", order_id, Order)

    def get_sku(self, sku_id: str) -> SKU:
        row = self.db.execute(
            """SELECT s.*, p.category FROM skus s JOIN spus p
            ON p.id=s.spu_id WHERE s.id=?""",
            (sku_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"不存在的 SKU：{sku_id}")
        values = dict(row)
        values["attrs"] = json.loads(values["attrs"])
        return SKU.model_validate(values)

    def get_line(self, order_id: str, order_item_id: str) -> Line:
        item = self._row("order_items", order_item_id, OrderItem)
        if item.order_id != order_id:
            raise ValueError("商品行不属于该订单")
        return Line(item, self.get_sku(item.sku_id))

    def _destination(self, order: Order, payment_method_id: str | None) -> str:
        if payment_method_id is not None and payment_method_id != order.payment_method_id:
            raise PolicyError(
                CheckResult(violations=[_issue("P15", "退款只能退回该订单原支付账户")])
            )
        payment = self._row("payment_methods", order.payment_method_id, PaymentMethod)
        result = check_refund_destination(order, payment)
        if not result.allowed:
            raise PolicyError(result)
        return payment.id

    def _aftersale(
        self,
        order: Order,
        item: OrderItem,
        kind: str,
        qty: int,
        amount: int,
        reason: str,
        *,
        new_sku_id: str | None = None,
        awaiting_payment: bool = False,
    ) -> str:
        record_id = next_id(self.db, "aftersales")
        insert(
            self.db,
            "aftersales",
            id=record_id,
            order_id=order.id,
            order_item_id=item.id,
            type=kind,
            status="awaiting_payment" if awaiting_payment else "completed",
            qty=qty,
            amount_cents=amount,
            new_sku_id=new_sku_id,
            reason=reason,
            created_at=self.clock.now().isoformat(),
        )
        return record_id

    def _refund(
        self,
        order: Order,
        amount: int,
        aftersale_id: str | None,
        payment_method_id: str | None = None,
    ) -> str:
        destination = self._destination(order, payment_method_id)
        _integer(amount)
        refunded = self.db.execute(
            "SELECT coalesce(sum(amount_cents),0) FROM refunds WHERE order_id=?", (order.id,)
        ).fetchone()[0]
        if amount + refunded > order.paid_cents:
            raise ValueError("累计退款不能超过订单实付")
        record_id = next_id(self.db, "refunds")
        insert(
            self.db,
            "refunds",
            id=record_id,
            order_id=order.id,
            aftersale_id=aftersale_id,
            amount_cents=amount,
            payment_method_id=destination,
            status="success",
            created_at=self.clock.now().isoformat(),
        )
        return record_id

    def _enforce(self, result: CheckResult) -> None:
        violations = (
            result.violations
            if self.enforce_policy
            else [
                issue
                for issue in result.violations
                if issue.rule_id in {"DATA", "STOCK", "P06", "P15"}
            ]
        )
        if violations:
            raise PolicyError(result.model_copy(update={"violations": violations}))

    def modify_address(self, order_id: str, address: Address) -> OperationResult:
        address = Address.model_validate(address)
        with transaction(self.db):
            order = self.get_order(order_id)
            self._enforce(check_modify_address(order))
            self.db.execute(
                "UPDATE orders SET address=? WHERE id=?", (address.model_dump_json(), order_id)
            )
        return OperationResult(order_id=order_id, status="updated")

    def cancel_order(
        self,
        order_id: str,
        reason: str,
        *,
        refund_payment_method_id: str | None = None,
    ) -> OperationResult:
        with transaction(self.db):
            order = self.get_order(order_id)
            self._destination(order, refund_payment_method_id)
            refunded = self.db.execute(
                "SELECT coalesce(sum(amount_cents),0) FROM refunds WHERE order_id=?", (order_id,)
            ).fetchone()[0]
            result = check_cancel_order(order, refunded)
            self._enforce(result)
            if order.status == "cancelled":
                raise ValueError("订单已取消，不能重复回补库存")
            record_ids = (
                [self._refund(order, result.amount_cents, None, refund_payment_method_id)]
                if order.paid_at
                else []
            )
            for row in self.db.execute(
                "SELECT sku_id, qty FROM order_items WHERE order_id=?", (order_id,)
            ).fetchall():
                self.db.execute(
                    "UPDATE skus SET stock=stock+? WHERE id=?", (row["qty"], row["sku_id"])
                )
            if order.user_coupon_id:
                coupon = self.db.execute(
                    """SELECT c.valid_to FROM user_coupons uc JOIN coupons c
                    ON c.id=uc.coupon_id WHERE uc.id=?""",
                    (order.user_coupon_id,),
                ).fetchone()
                status = (
                    "unused" if self.clock.now() <= datetime.fromisoformat(coupon[0]) else "expired"
                )
                self.db.execute(
                    "UPDATE user_coupons SET status=?, used_order_id=NULL WHERE id=?",
                    (status, order.user_coupon_id),
                )
            self.db.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
            if order.paid_at is not None:
                self.db.execute(
                    "UPDATE order_items SET refunded_cents=paid_share_cents WHERE order_id=?",
                    (order_id,),
                )
        return OperationResult(
            order_id=order_id,
            amount_cents=result.amount_cents,
            status="cancelled",
            record_ids=record_ids,
        )

    def apply_price_protection(
        self,
        order_id: str,
        order_item_id: str,
        *,
        refund_payment_method_id: str | None = None,
    ) -> OperationResult:
        with transaction(self.db):
            order = self.get_order(order_id)
            line = self.get_line(order_id, order_item_id)
            self._destination(order, refund_payment_method_id)
            result = check_price_protection(order, line, self.clock.now())
            self._enforce(result)
            record_id = self._aftersale(
                order, line.item, "price_protection", line.item.qty, result.amount_cents, "降价价保"
            )
            refund_id = self._refund(
                order, result.amount_cents, record_id, refund_payment_method_id
            )
            self.db.execute("UPDATE orders SET price_protected=1 WHERE id=?", (order_id,))
            self.db.execute(
                "UPDATE order_items SET refunded_cents=refunded_cents+? WHERE id=?",
                (result.amount_cents, order_item_id),
            )
        return OperationResult(
            order_id=order_id,
            amount_cents=result.amount_cents,
            status="completed",
            record_ids=[record_id, refund_id],
        )

    def request_return(
        self,
        order_id: str,
        items: list[ReturnItem],
        reason: str,
        *,
        refund_payment_method_id: str | None = None,
    ) -> OperationResult:
        requests = [ReturnItem.model_validate(item) for item in items]
        with transaction(self.db):
            order = self.get_order(order_id)
            self._destination(order, refund_payment_method_id)
            lines = [(self.get_line(order_id, item.order_item_id), item.qty) for item in requests]
            result = check_return(order, lines, self.clock.now())
            self._enforce(result)
            record_ids = []
            for line, qty in lines:
                amount = partial_refund(line.item, qty)
                record_id = self._aftersale(order, line.item, "return", qty, amount, reason)
                refund_id = self._refund(order, amount, record_id, refund_payment_method_id)
                self.db.execute(
                    """UPDATE order_items SET returned_qty=returned_qty+?,
                    refunded_cents=refunded_cents+?, status='returned' WHERE id=?""",
                    (qty, amount, line.item.id),
                )
                record_ids.extend([record_id, refund_id])
        return OperationResult(
            order_id=order_id,
            amount_cents=result.amount_cents,
            status="completed",
            record_ids=record_ids,
        )

    def request_exchange(
        self,
        order_id: str,
        order_item_id: str,
        new_sku_id: str,
        reason: str,
        *,
        refund_payment_method_id: str | None = None,
    ) -> OperationResult:
        with transaction(self.db):
            order = self.get_order(order_id)
            line = self.get_line(order_id, order_item_id)
            target = self.get_sku(new_sku_id)
            self._destination(order, refund_payment_method_id)
            result = check_exchange(order, line, target, self.clock.now())
            self._enforce(result)
            amount, top_up = exchange_amount(line.item, target.price_cents)
            qty = line.item.qty - line.item.returned_qty
            record_id = self._aftersale(
                order,
                line.item,
                "exchange",
                qty,
                amount,
                reason,
                new_sku_id=new_sku_id,
                awaiting_payment=top_up,
            )
            record_ids = [record_id]
            if not top_up and amount:
                record_ids.append(self._refund(order, amount, record_id, refund_payment_method_id))
                self.db.execute(
                    "UPDATE order_items SET refunded_cents=refunded_cents+? WHERE id=?",
                    (amount, order_item_id),
                )
            self.db.execute("UPDATE skus SET stock=stock-? WHERE id=?", (qty, new_sku_id))
            self.db.execute(
                "UPDATE order_items SET status='exchanged' WHERE id=?", (order_item_id,)
            )
        return OperationResult(
            order_id=order_id,
            amount_cents=amount,
            status="awaiting_payment" if top_up else "completed",
            record_ids=record_ids,
        )

    def create_order(
        self,
        user_id: str,
        items: list[PurchaseItem],
        payment_method_id: str,
        user_coupon_id: str | None = None,
        address: Address | None = None,
    ) -> OperationResult:
        requests = [PurchaseItem.model_validate(item) for item in items]
        if user_coupon_id is not None and not isinstance(user_coupon_id, str):
            raise PolicyError(CheckResult(violations=[_issue("P13", "每单只能指定一张优惠券")]))
        with transaction(self.db):
            user = self.db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if user is None:
                raise ValueError("用户不存在")
            payment = self._row("payment_methods", payment_method_id, PaymentMethod)
            # Payment ownership is a permanent account-integrity constraint.
            if payment.user_id != user_id:
                raise PolicyError(CheckResult(violations=[_issue("P15", "支付账户不属于该用户")]))
            lines = [(self.get_sku(item.sku_id), item.qty) for item in requests]
            coupons = []
            if user_coupon_id:
                owned = self._row("user_coupons", user_coupon_id, UserCoupon)
                coupons = [(owned, self._row("coupons", owned.coupon_id, Coupon))]
            result = check_create_order(user_id, lines, payment, coupons, self.clock.now())
            self._enforce(result)
            totals = [sku.price_cents * qty for sku, qty in lines]
            discount = sum(coupon.discount_cents for owned, coupon in coupons)
            shares = paid_shares(totals, discount)
            order_id = next_id(self.db, "orders")
            destination = (
                Address.model_validate(address)
                if address is not None
                else Address.model_validate_json(user["default_address"])
            )
            stamp = self.clock.now().isoformat()
            insert(
                self.db,
                "orders",
                id=order_id,
                user_id=user_id,
                status="pending_shipment",
                address=destination.model_dump_json(),
                note="",
                created_at=stamp,
                paid_at=stamp,
                shipped_at=None,
                delivered_at=None,
                items_total_cents=sum(totals),
                discount_cents=discount,
                paid_cents=sum(shares),
                payment_method_id=payment_method_id,
                user_coupon_id=user_coupon_id,
                price_protected=0,
            )
            for (sku, qty), share in zip(lines, shares, strict=True):
                insert(
                    self.db,
                    "order_items",
                    id=next_id(self.db, "order_items"),
                    order_id=order_id,
                    sku_id=sku.id,
                    qty=qty,
                    unit_price_cents=sku.price_cents,
                    paid_share_cents=share,
                    status="normal",
                    returned_qty=0,
                    refunded_cents=0,
                )
                self.db.execute("UPDATE skus SET stock=stock-? WHERE id=?", (qty, sku.id))
            if user_coupon_id:
                self.db.execute(
                    "UPDATE user_coupons SET status='used', used_order_id=? WHERE id=?",
                    (order_id, user_coupon_id),
                )
        return OperationResult(
            order_id=order_id, amount_cents=sum(shares), status="pending_shipment"
        )

    def submit_approval(
        self, order_id: str, request: ApprovalRequest, reason: str
    ) -> OperationResult:
        request = ApprovalRequest.model_validate(request)
        args = dict(request.args)
        if "order_id" in args and args["order_id"] != order_id:
            raise ValueError("Approval order_id conflicts with the target order")
        args["order_id"] = order_id
        with transaction(self.db):
            order = self.get_order(order_id)
            record_id = next_id(self.db, "approvals")
            insert(
                self.db,
                "approvals",
                id=record_id,
                user_id=order.user_id,
                order_id=order_id,
                action=request.action,
                args=json.dumps(args, ensure_ascii=False, sort_keys=True),
                reason=reason,
                status="pending",
                created_at=self.clock.now().isoformat(),
            )
        return OperationResult(order_id=order_id, status="pending", record_ids=[record_id])

    def transfer_to_human(self, user_id: str | None, summary: str, reason: str) -> str:
        """Record a handoff, including an unauthenticated request."""
        with transaction(self.db):
            record_id = next_id(self.db, "handoffs")
            insert(
                self.db,
                "handoffs",
                id=record_id,
                user_id=user_id,
                summary=summary,
                reason=reason,
                created_at=self.clock.now().isoformat(),
            )
        return record_id

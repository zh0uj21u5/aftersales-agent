"""Explicit domain snapshots and service request/result schemas."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

OrderStatus = Literal[
    "pending_payment",
    "pending_shipment",
    "shipped",
    "delivered",
    "completed",
    "aftersale",
    "cancelled",
]
Category = Literal["digital", "apparel", "home", "beauty", "fresh", "custom"]
Action = Literal["apply_price_protection", "request_return", "request_exchange"]


class Address(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    receiver_name: str = Field(min_length=1)
    phone: str = Field(min_length=1)
    province: str = Field(min_length=1)
    city: str = Field(min_length=1)
    district: str = Field(min_length=1)
    detail: str = Field(min_length=1)


class Snapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Order(Snapshot):
    id: str
    user_id: str
    status: OrderStatus
    address: Address
    note: str
    created_at: datetime
    paid_at: datetime | None
    shipped_at: datetime | None
    delivered_at: datetime | None
    items_total_cents: int
    discount_cents: int
    paid_cents: int
    payment_method_id: str
    user_coupon_id: str | None
    price_protected: bool


class OrderItem(Snapshot):
    id: str
    order_id: str
    sku_id: str
    qty: int
    unit_price_cents: int
    paid_share_cents: int
    status: Literal["normal", "returned", "exchanged"]
    returned_qty: int
    refunded_cents: int


class SKU(Snapshot):
    id: str
    spu_id: str
    attrs: dict[str, str]
    price_cents: int
    stock: int
    category: Category


class Coupon(Snapshot):
    id: str
    title: str
    threshold_cents: int
    discount_cents: int
    valid_from: datetime
    valid_to: datetime


class UserCoupon(Snapshot):
    id: str
    user_id: str
    coupon_id: str
    status: Literal["unused", "used", "expired"]
    used_order_id: str | None


class PaymentMethod(Snapshot):
    id: str
    user_id: str
    type: Literal["alipay", "wechat", "card"]
    label: str


class ReturnItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    order_item_id: str
    qty: int = Field(gt=0)


class PurchaseItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    sku_id: str
    qty: int = Field(gt=0)


class Violation(BaseModel):
    model_config = ConfigDict(frozen=True)
    rule_id: str
    reason: str


class CheckResult(BaseModel):
    violations: list[Violation] = Field(default_factory=list)
    amount_cents: int = 0
    needs_approval: bool = False

    @property
    def allowed(self) -> bool:
        return not self.violations


class OperationResult(BaseModel):
    order_id: str
    amount_cents: int = 0
    status: str
    record_ids: list[str] = Field(default_factory=list)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Action
    args: dict[str, JsonValue]

    @field_validator("args")
    @classmethod
    def nonempty_args(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if not value:
            raise ValueError("Approval arguments cannot be empty")
        return value


@dataclass(frozen=True)
class Line:
    item: OrderItem
    sku: SKU


class User(Snapshot):
    id: str
    name: str
    phone: str
    default_address: Address


class UserDetails(User):
    payment_methods: list[PaymentMethod]


class AuthResult(BaseModel):
    status: Literal["authenticated", "not_found"]
    user_id: str | None = None


class Shipment(Snapshot):
    order_id: str
    carrier: str
    tracking_no: str
    status: str


class OrderDetails(BaseModel):
    order: Order
    items: list[OrderItem]
    shipment: Shipment | None = None


class Product(Snapshot):
    id: str
    title: str
    category: Category
    description: str
    skus: list[SKU]


class PricePoint(Snapshot):
    sku_id: str
    price_cents: int
    effective_at: datetime


class CouponDetails(BaseModel):
    user_coupon: UserCoupon
    coupon: Coupon


RefundAction = Literal[
    "cancel_order", "apply_price_protection", "request_return", "request_exchange"
]


class RefundPreview(CheckResult):
    direction: Literal["refund", "top_up"] = "refund"
    amount_display: str

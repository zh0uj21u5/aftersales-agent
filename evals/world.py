"""Deterministic external events for the SQLite evaluation environment."""

import sqlite3
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aftersales.domain.clock import Clock
from aftersales.domain.db import transaction


class ScheduledEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    after_turn: int = Field(ge=1, le=19)


class ShipOrder(ScheduledEvent):
    kind: Literal["ship_order"]
    order_id: str
    tracking_no: str = Field(default="T99990001", min_length=1)


class SetSkuStock(ScheduledEvent):
    kind: Literal["set_sku_stock"]
    sku_id: str
    stock: int = Field(ge=0)


class SetSkuPrice(ScheduledEvent):
    kind: Literal["set_sku_price"]
    sku_id: str
    price_cents: int = Field(gt=0)


class SetCouponStatus(ScheduledEvent):
    kind: Literal["set_coupon_status"]
    user_coupon_id: str
    status: Literal["used", "expired"]
    used_order_id: str | None = None

    @model_validator(mode="after")
    def check_order_reference(self) -> "SetCouponStatus":
        if (self.status == "used") != bool(self.used_order_id):
            raise ValueError("Only a used coupon must reference an order")
        if self.status == "expired" and self.used_order_id is not None:
            raise ValueError("An expired coupon must have a null order reference")
        return self


WorldEvent = Annotated[
    ShipOrder | SetSkuStock | SetSkuPrice | SetCouponStatus, Field(discriminator="kind")
]


class WorldEventConflict(ValueError):
    """The actual world no longer contains an applicable target."""


def apply_world_event(db: sqlite3.Connection, clock: Clock, event: WorldEvent) -> None:
    """Apply one event atomically; fail if the specified world cannot be reached."""
    with transaction(db):
        if isinstance(event, ShipOrder):
            timestamp = clock.now().isoformat()
            changed = db.execute(
                """UPDATE orders SET status='shipped', shipped_at=?
                WHERE id=? AND status='pending_shipment'""",
                (timestamp, event.order_id),
            )
            if changed.rowcount == 1:
                db.execute(
                    "INSERT INTO shipments VALUES (?, '评测物流', ?, 'shipped')",
                    (event.order_id, event.tracking_no),
                )
        elif isinstance(event, SetSkuStock):
            changed = db.execute("UPDATE skus SET stock=? WHERE id=?", (event.stock, event.sku_id))
        elif isinstance(event, SetSkuPrice):
            changed = db.execute(
                "UPDATE skus SET price_cents=? WHERE id=?", (event.price_cents, event.sku_id)
            )
            if changed.rowcount == 1:
                db.execute(
                    "INSERT INTO price_history VALUES (?, ?, ?)",
                    (event.sku_id, event.price_cents, clock.now().isoformat()),
                )
        else:
            changed = db.execute(
                """UPDATE user_coupons SET status=?, used_order_id=?
                WHERE id=? AND (? IS NULL OR EXISTS (
                    SELECT 1 FROM orders o WHERE o.id=? AND o.user_id=user_coupons.user_id
                    AND (o.user_coupon_id IS NULL OR o.user_coupon_id=user_coupons.id)
                ))""",
                (
                    event.status,
                    event.used_order_id,
                    event.user_coupon_id,
                    event.used_order_id,
                    event.used_order_id,
                ),
            )
        if changed.rowcount != 1:
            raise WorldEventConflict(
                f"World event {event.kind} could not update its target: {event}"
            )

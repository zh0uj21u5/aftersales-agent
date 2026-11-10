"""Thin transaction tool wrappers around domain services."""

from aftersales.domain.models import Address, OperationResult, PurchaseItem, ReturnItem
from aftersales.tools.registry import ToolContext, tool


@tool(name="modify_address", permission="write", policy_topics=["address_cancel"])
def modify_address(ctx: ToolContext, order_id: str, address: Address) -> OperationResult:
    """Change the destination of an eligible order."""
    return ctx.services().modify_address(order_id, address)


@tool(name="cancel_order", permission="write", policy_topics=["address_cancel", "coupon"])
def cancel_order(ctx: ToolContext, order_id: str, reason: str) -> OperationResult:
    """Cancel an eligible order and refund its remaining payment."""
    return ctx.services().cancel_order(order_id, reason)


@tool(name="apply_price_protection", permission="write", policy_topics=["price_protection"])
def apply_price_protection(ctx: ToolContext, order_id: str, order_item_id: str) -> OperationResult:
    """Refund a qualifying price difference to the original payment method."""
    return ctx.services().apply_price_protection(order_id, order_item_id)


@tool(name="request_return", permission="write", policy_topics=["return", "refund"])
def request_return(
    ctx: ToolContext, order_id: str, items: list[ReturnItem], reason: str
) -> OperationResult:
    """Return eligible quantities and refund their remaining paid share."""
    return ctx.services().request_return(order_id, items, reason)


@tool(name="request_exchange", permission="write", policy_topics=["exchange"])
def request_exchange(
    ctx: ToolContext, order_id: str, order_item_id: str, new_sku_id: str, reason: str
) -> OperationResult:
    """Exchange an eligible line for another variant of its product."""
    return ctx.services().request_exchange(order_id, order_item_id, new_sku_id, reason)


@tool(name="create_order", permission="write", policy_topics=["coupon"])
def create_order(
    ctx: ToolContext,
    user_id: str,
    items: list[PurchaseItem],
    payment_method_id: str,
    user_coupon_id: str | None = None,
    address: Address | None = None,
) -> OperationResult:
    """Place and pay for an order using stock and at most one coupon."""
    return ctx.services().create_order(user_id, items, payment_method_id, user_coupon_id, address)


WRITE_TOOLS = (
    modify_address,
    cancel_order,
    apply_price_protection,
    request_return,
    request_exchange,
    create_order,
)

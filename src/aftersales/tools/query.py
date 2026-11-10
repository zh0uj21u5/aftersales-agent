"""Read-only domain queries and policy lookup."""

from typing import Literal

from aftersales.domain.calculator import calculate as safe_calculate
from aftersales.domain.models import (
    CouponDetails,
    Order,
    OrderDetails,
    OrderStatus,
    PricePoint,
    Product,
    RefundAction,
    RefundPreview,
    UserDetails,
)
from aftersales.policy.document import PolicyTopic, lookup_topic
from aftersales.policy.proposal import ActionRequest, ProposalResult, create_proposal
from aftersales.tools.registry import ToolContext, tool


@tool(name="get_user", policy_topics=["auth"])
def get_user(ctx: ToolContext, user_id: str) -> UserDetails:
    """Read a customer profile, default address and masked payment methods."""
    return ctx.services().get_user(user_id)


@tool(name="list_orders", policy_topics=["auth"])
def list_orders(ctx: ToolContext, user_id: str, status: OrderStatus | None = None) -> list[Order]:
    """List a customer's orders, optionally filtered by status."""
    return ctx.services().list_orders(user_id, status)


@tool(name="get_order", policy_topics=["address_cancel"])
def get_order(ctx: ToolContext, order_id: str) -> OrderDetails:
    """Read an order with item IDs, paid shares and shipment information."""
    return ctx.services().order_details(order_id)


@tool(name="get_product", policy_topics=["exchange"])
def get_product(ctx: ToolContext, spu_id: str | None = None, sku_id: str | None = None) -> Product:
    """Read product details and variants; supply exactly one of spu_id or sku_id."""
    return ctx.services().get_product(spu_id, sku_id)


@tool(name="get_price_history", policy_topics=["price_protection"])
def get_price_history(ctx: ToolContext, sku_id: str) -> list[PricePoint]:
    """Read historical prices ordered by their effective time."""
    return ctx.services().get_price_history(sku_id)


@tool(name="list_user_coupons", policy_topics=["coupon"])
def list_user_coupons(
    ctx: ToolContext, user_id: str, status: Literal["unused", "used", "expired"] | None = None
) -> list[CouponDetails]:
    """Read owned coupons with their thresholds, discounts and validity windows."""
    return ctx.services().list_user_coupons(user_id, status)


@tool(name="lookup_policy", policy_topics=["security"])
def lookup_policy(ctx: ToolContext, topic: PolicyTopic) -> str:
    """Read all policy sections belonging to the requested topic."""
    if topic == "confirm" and ctx.config.confirm_gate and ctx.config.proposal_gate:
        from aftersales.core.context import with_proposal_contract

        return with_proposal_contract(lookup_topic(topic), True)
    return lookup_topic(topic)


@tool(name="calculate", policy_topics=["refund"])
def calculate(ctx: ToolContext, expression: str) -> str:
    """Calculate + - * / and parentheses safely; repeating decimals use 50-digit precision."""
    return safe_calculate(expression)


@tool(name="preview_refund", policy_topics=["refund", "approval"])
def preview_refund(
    ctx: ToolContext,
    order_id: str,
    action: RefundAction,
    order_item_id: str | None = None,
    qty: int | None = None,
    new_sku_id: str | None = None,
) -> RefundPreview:
    """Preview a refund or exchange top-up and policy violations without writing data."""
    return ctx.services().preview_refund(order_id, action, order_item_id, qty, new_sku_id)


@tool(name="propose_actions", permission="read", policy_topics=["confirm", "approval"])
def propose_actions(ctx: ToolContext, actions: list[ActionRequest]) -> ProposalResult:
    """Validate independent write actions and preview exact cents; save a whole-plan session proposal.

    Each action supplies tool_name and arguments matching that write tool's schema.
    Present the returned canonical arguments and amounts now, then wait for whole-plan consent.
    Partial consent or edits require a new proposal and fresh confirmation. P08 candidates
    require submit_approval instead of direct execution. Dependent operations need separate plans.
    """
    return create_proposal(ctx, actions)

"""Resource ownership and shared domain policy checks before execution."""

from pydantic import BaseModel

from aftersales.core.hooks import Allow, Deny
from aftersales.core.llm import ToolCall
from aftersales.domain.models import CheckResult, Violation
from aftersales.domain.services import (
    check_cancel_order,
    check_exchange,
    check_modify_address,
    check_price_protection,
    check_return,
)
from aftersales.tools.registry import ToolContext

AUTH_TOOLS = {"find_user_by_phone", "find_user_by_order"}
PUBLIC_TOOLS = AUTH_TOOLS | {"lookup_policy", "transfer_to_human", "update_todo"}


def check_access(ctx: ToolContext, call: ToolCall) -> Allow | Deny:
    """Authentication tools verify their own credentials before assigning identity."""
    if call.name in PUBLIC_TOOLS:
        return Allow()
    user_id = ctx.session.authenticated_user_id
    if user_id is None:
        return Deny("请先验证身份", ["P01"])
    if "user_id" in call.arguments and call.arguments["user_id"] != user_id:
        return Deny("只能访问本人资源", ["P01"])
    if "order_id" in call.arguments:
        row = (
            ctx.services()
            .db.execute("SELECT user_id FROM orders WHERE id=?", (call.arguments["order_id"],))
            .fetchone()
        )
        if row is None or row[0] != user_id:
            return Deny("订单不存在或不属于当前用户", ["P01"])
    return Allow()


def business_check(ctx: ToolContext, name: str, args: BaseModel) -> CheckResult:
    """Load snapshots; all business decisions live in services.check_* functions."""
    service = ctx.services()
    if name == "create_order":
        result = service.check_purchase(
            args.user_id, args.items, args.payment_method_id, args.user_coupon_id
        )
    elif name in {
        "modify_address",
        "cancel_order",
        "apply_price_protection",
        "request_return",
        "request_exchange",
    }:
        order = service.get_order(args.order_id)
        if name == "modify_address":
            result = check_modify_address(order)
        elif name == "cancel_order":
            result = check_cancel_order(order, service.refunded_cents(order.id))
        elif name == "apply_price_protection":
            result = check_price_protection(
                order, service.get_line(order.id, args.order_item_id), service.clock.now()
            )
        elif name == "request_return":
            result = check_return(
                order,
                [(service.get_line(order.id, item.order_item_id), item.qty) for item in args.items],
                service.clock.now(),
            )
        else:
            result = check_exchange(
                order,
                service.get_line(order.id, args.order_item_id),
                service.get_sku(args.new_sku_id),
                service.clock.now(),
            )
    else:
        return CheckResult(violations=[Violation(rule_id="DATA", reason="写工具未配置规则校验")])
    return result


def check_business(ctx: ToolContext, name: str, args: BaseModel) -> Allow | Deny:
    result = business_check(ctx, name, args)
    if result.allowed:
        return Allow()
    return Deny(
        "；".join(issue.reason for issue in result.violations),
        list(dict.fromkeys(issue.rule_id for issue in result.violations)),
    )

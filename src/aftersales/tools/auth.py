"""Identity checks against trusted customer records."""

from aftersales.domain.models import AuthResult
from aftersales.tools.registry import ToolContext, tool


@tool(name="find_user_by_phone", policy_topics=["auth"])
def find_user_by_phone(ctx: ToolContext, phone: str, name: str) -> AuthResult:
    """Authenticate using the exact phone number and customer name."""
    result = ctx.services().find_user_by_phone(phone, name)
    ctx.session.authenticated_user_id = result.user_id
    return result


@tool(name="find_user_by_order", policy_topics=["auth"])
def find_user_by_order(ctx: ToolContext, order_id: str, receiver_name: str) -> AuthResult:
    """Authenticate using an order number and its receiver name."""
    result = ctx.services().find_user_by_order(order_id, receiver_name)
    ctx.session.authenticated_user_id = result.user_id
    return result

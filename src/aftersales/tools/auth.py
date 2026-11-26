"""Identity checks against trusted customer records."""

from aftersales.domain.models import AuthResult, CheckResult, Violation
from aftersales.domain.services import PolicyError
from aftersales.tools.registry import ToolContext, tool


def _check_identity(ctx: ToolContext) -> None:
    if getattr(ctx.session, "owner_user_id", None) is not None:
        raise PolicyError(
            CheckResult(
                violations=[
                    Violation(rule_id="P01", reason="已绑定登录身份，不能通过工具重新认证"),
                ]
            )
        )


@tool(name="find_user_by_phone", policy_topics=["auth"])
def find_user_by_phone(ctx: ToolContext, phone: str, name: str) -> AuthResult:
    """Authenticate using the exact phone number and customer name."""
    _check_identity(ctx)
    result = ctx.services().find_user_by_phone(phone, name)
    ctx.session.authenticated_user_id = result.user_id
    return result


@tool(name="find_user_by_order", policy_topics=["auth"])
def find_user_by_order(ctx: ToolContext, order_id: str, receiver_name: str) -> AuthResult:
    """Authenticate using an order number and its receiver name."""
    _check_identity(ctx)
    result = ctx.services().find_user_by_order(order_id, receiver_name)
    ctx.session.authenticated_user_id = result.user_id
    return result

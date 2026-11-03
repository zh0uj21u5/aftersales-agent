"""Approval and human handoff tools."""

from pydantic import JsonValue

from aftersales.domain.models import Action, ApprovalRequest, OperationResult
from aftersales.tools.registry import ToolContext, tool


@tool(name="submit_approval", permission="escalate", policy_topics=["approval"])
def submit_approval(
    ctx: ToolContext, order_id: str, action: Action, args: dict[str, JsonValue], reason: str
) -> OperationResult:
    """Submit a pending human approval for a large after-sales refund."""
    request = ApprovalRequest.model_validate({"action": action, "args": args})
    return ctx.services().submit_approval(order_id, request, reason)


@tool(name="transfer_to_human", permission="escalate", policy_topics=["escalation"])
def transfer_to_human(ctx: ToolContext, summary: str, reason: str) -> dict[str, str]:
    """Record a handoff and end this conversation."""
    record_id = ctx.services().transfer_to_human(ctx.session.authenticated_user_id, summary, reason)
    return {"status": "transferred", "handoff_id": record_id}

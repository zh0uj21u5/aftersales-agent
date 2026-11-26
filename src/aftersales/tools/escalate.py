"""Approval and human handoff tools."""

from pydantic import JsonValue

from aftersales.domain.db import is_postgres, transaction
from aftersales.domain.models import Action, ApprovalRequest, OperationResult
from aftersales.domain.services import PolicyError
from aftersales.policy.guard import business_check
from aftersales.policy.proposal import _fail, retire_submitted_approval, validate_approval_arguments
from aftersales.tools.registry import ToolContext, tool


@tool(name="submit_approval", permission="escalate", policy_topics=["approval"])
def submit_approval(
    ctx: ToolContext, order_id: str, action: Action, args: dict[str, JsonValue], reason: str
) -> OperationResult:
    """Submit a pending human approval for a large after-sales refund."""
    request = ApprovalRequest.model_validate({"action": action, "args": args})
    service = ctx.services()
    if is_postgres(service.db):
        validated = validate_approval_arguments(action, args, order_id, reason)
        with (
            transaction(service.db),
            service.lock_action_resources(
                order_id,
                [validated.new_sku_id] if action == "request_exchange" else [],
                None,
            ),
        ):
            check = business_check(ctx, action, validated, service=service)
            if any(issue.rule_id != "P08" for issue in check.violations):
                raise PolicyError(check)
            if not check.needs_approval:
                raise _fail("该操作未达到人工审批门槛", "P08")
            result = service.submit_approval(
                order_id,
                request,
                reason,
                session_id=ctx.session.session_id,
                requested_amount_cents=check.amount_cents,
            )
    else:
        result = service.submit_approval(order_id, request, reason)
    if ctx.config.proposal_gate:
        retire_submitted_approval(ctx.session, order_id, request)
    return result


@tool(name="transfer_to_human", permission="escalate", policy_topics=["escalation"])
def transfer_to_human(ctx: ToolContext, summary: str, reason: str) -> dict[str, str]:
    """Record a handoff and end this conversation."""
    record_id = ctx.services().transfer_to_human(ctx.session.authenticated_user_id, summary, reason)
    return {"status": "transferred", "handoff_id": record_id}

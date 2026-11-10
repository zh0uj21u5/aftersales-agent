"""Session-owned proposals, whole-plan authorization and single-use binding."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aftersales.domain.models import CheckResult, Violation
from aftersales.domain.services import PolicyError

if TYPE_CHECKING:
    from aftersales.tools.registry import SessionState, ToolContext

WriteToolName = Literal[
    "modify_address",
    "cancel_order",
    "apply_price_protection",
    "request_return",
    "request_exchange",
    "create_order",
]


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tool_name: WriteToolName
    arguments: dict[str, Any]


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action_id: str
    tool_name: WriteToolName
    canonical_args: dict[str, Any]
    preview_amount_cents: int | None
    requires_approval: bool
    preconditions: dict[str, Any]
    status: Literal["pending", "approved", "executed", "invalidated"] = "pending"


class ActionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    proposal_id: str
    version: int = Field(ge=1)
    session_id: str
    user_id: str
    status: Literal["pending", "approved", "rejected", "superseded", "consumed", "expired"]
    confirm_source: Literal["natural_language", "web_button"] | None = None
    actions: list[ProposedAction] = Field(min_length=1)
    created_at: str


class ProposalResult(BaseModel):
    proposal: ActionProposal
    guidance: list[str]


def _fail(reason: str, rule_id: str = "P02") -> PolicyError:
    return PolicyError(CheckResult(violations=[Violation(rule_id=rule_id, reason=reason)]))


def business_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in arguments.items() if key != "reason"}


def invalidate(
    proposal: ActionProposal, status: Literal["rejected", "superseded", "expired"]
) -> None:
    proposal.status = status
    for action in proposal.actions:
        if action.status != "executed":
            action.status = "invalidated"


def expire_identity(session: SessionState) -> None:
    for proposal in session.proposals:
        if proposal.status in {"pending", "approved"} and (
            proposal.user_id != session.authenticated_user_id
            or proposal.session_id != session.session_id
        ):
            invalidate(proposal, "expired")


def latest_proposal(session: SessionState) -> ActionProposal | None:
    expire_identity(session)
    return session.proposals[-1] if session.proposals else None


def validate_arguments(name: WriteToolName, arguments: dict[str, Any]) -> BaseModel:
    """Use the very same strict schemas as the executable write tools."""
    from aftersales.tools.registry import ToolRegistry
    from aftersales.tools.write import WRITE_TOOLS

    entry = ToolRegistry(WRITE_TOOLS).get(name)
    if entry is None:
        raise _fail("不支持的提案动作", "DATA")
    try:
        args = entry.arguments_model.model_validate(arguments, strict=True)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_url=False, include_context=False)
        raise _fail(f"提案参数不符合 {name} 的严格 Schema：{errors}", "DATA") from exc
    if hasattr(args, "reason") and not args.reason.strip():
        raise _fail("reason 必须是非空字符串")
    return args


def preconditions(ctx: ToolContext, name: WriteToolName, args: BaseModel) -> dict[str, Any]:
    """Capture only action-relevant inputs; an address edit must not invalidate price protection."""
    service = ctx.services()
    state: dict[str, Any] = {}
    if name == "create_order":
        user = service.get_user(args.user_id)
        state["skus"] = [
            service.get_sku(item.sku_id).model_dump(mode="json") for item in args.items
        ]
        state["payment"] = next(
            (
                p.model_dump(mode="json")
                for p in user.payment_methods
                if p.id == args.payment_method_id
            ),
            None,
        )
        if args.address is None:
            state["default_address"] = user.default_address.model_dump(mode="json")
        if args.user_coupon_id:
            state["coupon"] = next(
                (
                    c.model_dump(mode="json")
                    for c in service.list_user_coupons(args.user_id)
                    if c.user_coupon.id == args.user_coupon_id
                ),
                None,
            )
        return state
    order = service.get_order(args.order_id)
    fields = {"id", "user_id", "status"}
    if name == "modify_address":
        fields.add("address")
    else:
        fields |= {"paid_cents", "payment_method_id", "paid_at"}
    if name == "apply_price_protection":
        fields |= {"price_protected", "delivered_at"}
    elif name in {"request_return", "request_exchange"}:
        fields.add("delivered_at")
    elif name == "cancel_order":
        fields.add("user_coupon_id")
        state["refunded_cents"] = service.refunded_cents(order.id)
        if order.user_coupon_id:
            state["coupon"] = next(
                c.model_dump(mode="json")
                for c in service.list_user_coupons(order.user_id)
                if c.user_coupon.id == order.user_coupon_id
            )
    state["order"] = order.model_dump(mode="json", include=fields)
    if name == "cancel_order":
        state["items"] = [
            item.model_dump(mode="json") for item in service.order_details(order.id).items
        ]
    elif name in {"apply_price_protection", "request_return", "request_exchange"}:
        ids = (
            [item.order_item_id for item in args.items]
            if name == "request_return"
            else [args.order_item_id]
        )
        lines = [service.get_line(order.id, item_id) for item_id in ids]
        state["items"] = [line.item.model_dump(mode="json") for line in lines]
        sku_fields = {"id", "spu_id", "category"}
        if name == "apply_price_protection":
            sku_fields.add("price_cents")
        state["source_skus"] = [
            line.sku.model_dump(mode="json", include=sku_fields) for line in lines
        ]
        if name == "request_exchange":
            state["target_sku"] = service.get_sku(args.new_sku_id).model_dump(mode="json")
    return state


def create_proposal(ctx: ToolContext, actions: list[ActionRequest]) -> ProposalResult:
    from aftersales.core.hooks import Deny
    from aftersales.core.llm import ToolCall
    from aftersales.policy.guard import business_check, check_access

    session = ctx.session
    latest_proposal(session)
    if session.authenticated_user_id is None:
        raise _fail("请先验证身份再生成方案", "P01")
    if not actions:
        raise _fail("方案必须包含至少一个动作", "DATA")
    prepared: list[ProposedAction] = []
    for index, request in enumerate(actions, 1):
        args = validate_arguments(request.tool_name, request.arguments)
        canonical = args.model_dump(mode="json")
        access = check_access(
            ctx, ToolCall(id="proposal", name=request.tool_name, arguments=canonical)
        )
        if isinstance(access, Deny):
            raise _fail(access.reason, "P01")
        try:
            check = business_check(ctx, request.tool_name, args)
            snapshot = preconditions(ctx, request.tool_name, args)
        except ValueError as exc:
            raise _fail("资源不存在或参数关系不正确；请查询后重新分步提案", "DATA") from exc
        # P08-only actions are informational approval candidates, never executable refunds.
        illegal = [issue for issue in check.violations if issue.rule_id != "P08"]
        if illegal:
            raise PolicyError(
                CheckResult(
                    violations=[
                        issue.model_copy(
                            update={
                                "reason": issue.reason
                                + "；请按当前状态分步提案，前序执行后再生成下一方案"
                            }
                        )
                        for issue in illegal
                    ]
                )
            )
        if any(
            a.tool_name == request.tool_name
            and business_arguments(a.canonical_args) == business_arguments(canonical)
            for a in prepared
        ):
            raise _fail("方案中不能重复同一动作；请合并或分步提案")
        prepared.append(
            ProposedAction(
                action_id=f"ACT{index:02}",
                tool_name=request.tool_name,
                canonical_args=canonical,
                preview_amount_cents=None
                if request.tool_name == "modify_address"
                else check.amount_cents,
                requires_approval=check.needs_approval,
                preconditions=snapshot,
            )
        )
    version = session.proposals[-1].version + 1 if session.proposals else 1
    proposal = ActionProposal(
        proposal_id=f"P{version:04}",
        version=version,
        session_id=session.session_id,
        user_id=session.authenticated_user_id,
        status="pending",
        actions=prepared,
        created_at=ctx.services().clock.now().isoformat(),
    )
    for old in session.proposals:
        if old.status != "consumed":
            invalidate(old, "superseded")
    session.proposals.append(proposal)
    session.presented_proposal_version = None
    guidance = [
        "请在本轮直接复述整份方案的完整参数与精确金额并请求确认；部分同意或修改须重新提案并再次确认。"
    ]
    if any(action.requires_approval for action in prepared):
        guidance.append(
            "[P08] requires_approval=true 的动作超过20000分，禁止直接执行退款；应改走 submit_approval，审批 pending 不代表已退款。"
        )
    return ProposalResult(proposal=proposal.model_copy(deep=True), guidance=guidance)


def decide_proposal(
    session: SessionState,
    proposal_id: str,
    version: int,
    decision: Literal["approve", "reject"],
    *,
    source: Literal["natural_language", "web_button"] = "web_button",
) -> ActionProposal:
    if session.closed:
        raise _fail("会话已结束，不能确认新动作")
    proposal = latest_proposal(session)
    if (
        proposal is None
        or proposal.status != "pending"
        or proposal.proposal_id != proposal_id
        or type(version) is not int
        or proposal.version != version
    ):
        raise _fail("方案标识、版本或待确认状态不匹配；请读取最新方案并重新确认")
    if decision not in {"approve", "reject"} or source not in {"natural_language", "web_button"}:
        raise _fail("不支持的方案决策或来源")
    proposal.confirm_source = source
    if decision == "reject":
        invalidate(proposal, "rejected")
    else:
        proposal.status = "approved"
        for action in proposal.actions:
            action.status = "approved"
    session.persist()
    return proposal.model_copy(deep=True)


def changes_plan(text: str, proposal: ActionProposal) -> bool:
    from aftersales.core.hooks import has_veto

    if has_veto(text):
        return True
    if any(a.tool_name == "cancel_order" for a in proposal.actions) and "不取消订单" in text:
        return True
    return bool(
        re.search(
            r"只(?:改|退|换|取消|执行|做|办理|要|申请|处理)|仅|部分|除外|排除|保留|暂|稍后|晚点|再说|"
            r"先(?:改|退|换|取消|执行|做|办理)|如果|假如|若|只要|除非|前提|条件|但是|不过|但|"
            r"改成|换成|改为|换为|改到|换到|修改|另一个|另一|加上|还要|顺便|地址[为是：:]|"
            r"\b(?:only|except|if|unless|but|change|instead|also|add)\b",
            text,
            re.IGNORECASE,
        )
    )


def whole_plan_acknowledgment(text: str) -> bool:
    """Fail closed on unrecognized additions instead of treating any embedded YES as consent."""
    from aftersales.core.hooks import confirmation_text

    text = re.sub(r"[\s，。；！、,.!;]+", "", confirmation_text(text)).lower()
    return bool(
        re.fullmatch(
            r"(?:好的?|可以|我?确认|我?同意|是的|行|没问题|ok|yes|就这么办|就这样办|照办|"
            r"按(?:照)?(?:上述|以上|当前|这份|整份|全部|原)?方案(?:全部)?(?:执行|办理)|"
            r"(?:整份|全部|都)(?:同意|确认|执行|办理)|执行|办理|请|吧)+",
            text,
        )
    )


def authorize_natural_language(session: SessionState, agrees: Callable[[], bool]) -> None:
    """Approve only a presented pending version; amendments revoke even residual authorization."""
    proposal = latest_proposal(session)
    if proposal is None or proposal.status not in {"pending", "approved"}:
        return
    index = session.confirmation_user_index
    if index < 0:
        return
    text = session.messages[index].content or ""
    if changes_plan(text, proposal):
        proposal.confirm_source = "natural_language"
        invalidate(proposal, "rejected")
    elif (
        proposal.status == "pending"
        and session.confirmation_proposal
        and session.presented_proposal_version == proposal.version
        and whole_plan_acknowledgment(text)
        and agrees()
    ):
        decide_proposal(
            session, proposal.proposal_id, proposal.version, "approve", source="natural_language"
        )


def bind_action(ctx: ToolContext, name: str, args: BaseModel) -> ProposedAction:
    from aftersales.core.hooks import Deny
    from aftersales.core.llm import ToolCall
    from aftersales.policy.guard import business_check, check_access

    proposal = latest_proposal(ctx.session)
    if proposal is None:
        raise _fail("未先生成方案；请先 propose_actions，再复述并征求用户确认")
    label = f"{proposal.proposal_id} v{proposal.version}"
    if proposal.status != "approved":
        raise _fail(f"方案 {label} 尚未获整份授权或已失效/消费；请重新 propose_actions 并征求确认")
    canonical = args.model_dump(mode="json")
    if hasattr(args, "reason") and not args.reason.strip():
        raise _fail("reason 必须是非空字符串")
    business_args = business_arguments(canonical)
    action = next(
        (
            a
            for a in proposal.actions
            if a.status == "approved"
            and a.tool_name == name
            and business_arguments(a.canonical_args) == business_args
        ),
        None,
    )
    if action is None:
        raise _fail(
            f"调用参数或动作与已确认方案 {label} 不一致，或授权已消费；请重新 propose_actions 并征求确认"
        )
    access = check_access(ctx, ToolCall(id="binding", name=name, arguments=canonical))
    if isinstance(access, Deny):
        invalidate(proposal, "expired")
        raise _fail(access.reason, "P01")
    try:
        check = business_check(ctx, name, args)
        snapshot = preconditions(ctx, action.tool_name, args)
    except ValueError as exc:
        invalidate(proposal, "expired")
        raise _fail(f"方案 {label} 的业务前提已失效；请重新 propose_actions 并征求确认") from exc
    if not check.allowed:
        invalidate(proposal, "expired")
        raise PolicyError(
            check.model_copy(
                update={
                    "violations": [
                        issue.model_copy(
                            update={
                                "reason": f"方案 {label} 的业务前提已失效：{issue.reason}；请重新 propose_actions 并征求确认"
                            }
                        )
                        for issue in check.violations
                    ]
                }
            )
        )
    amount = None if name == "modify_address" else check.amount_cents
    if snapshot != action.preconditions or amount != action.preview_amount_cents:
        invalidate(proposal, "expired")
        raise _fail(f"方案 {label} 的业务前提或金额已发生变化；请重新 propose_actions 并征求确认")
    return action


def consume_action(session: SessionState, action: ProposedAction, *, success: bool) -> None:
    proposal = latest_proposal(session)
    if proposal is None or proposal.status != "approved" or action not in proposal.actions:
        return
    if not success:
        invalidate(proposal, "expired")
        return
    action.status = "executed"
    if all(item.status == "executed" for item in proposal.actions):
        proposal.status = "consumed"

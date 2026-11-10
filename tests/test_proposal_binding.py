"""Deterministic proposal contracts against MockLLM and the real seeded domain."""

import json
from pathlib import Path
from typing import Any

import pytest

from aftersales.core.config import AgentConfig
from aftersales.core.context import prepare_messages
from aftersales.core.hooks import PolicyHooks
from aftersales.core.llm import LLMResponse, Message, MockLLM, ToolCall
from aftersales.core.loop import Agent, Session
from aftersales.core.runtime import build_system
from aftersales.core.tracing import TraceWriter, read_trace
from aftersales.domain.db import dump_hashes
from aftersales.domain.services import PolicyError, Services
from aftersales.policy.proposal import decide_proposal, presentation_matches
from aftersales.tools import build_registry
from aftersales.tools.registry import ToolContext
from evals.models import load_tasks
from evals.runner import run_trial


def call(tool_name: str, **args: Any) -> ToolCall:
    return ToolCall(id=tool_name, name=tool_name, arguments=args)


def using(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(tool_calls=list(calls), stop_reason="tool_use")


def action(name: str, **args: Any) -> dict[str, Any]:
    return {"tool_name": name, "arguments": args}


def propose(*actions: dict[str, Any]) -> LLMResponse:
    return using(call("propose_actions", actions=list(actions)))


def address_action(shop: Services, order_id: str = "O1006") -> dict[str, Any]:
    address = shop.get_order(order_id).address.model_dump()
    address["detail"] = "公司88号"
    return action("modify_address", order_id=order_id, address=address)


def as_call(request: dict[str, Any]) -> ToolCall:
    return call(request["tool_name"], **request["arguments"])


def setup_agent(
    shop: Services,
    script: list[LLMResponse],
    *,
    user: str | None = "U006",
    config: AgentConfig | None = None,
    trace: TraceWriter | None = None,
    classifier: MockLLM | None = None,
) -> tuple[Agent, Session, MockLLM]:
    config = config or AgentConfig(proposal_gate=True)
    registry = build_registry(config)
    mock = MockLLM(script)
    agent = Agent(
        mock,
        registry,
        PolicyHooks(registry, trace=trace, classifier=classifier),
        config,
        lambda s: ToolContext(s, config, shop.db, shop.clock),
        system=lambda: build_system(shop.clock, config.policy_mode),
        trace=trace,
    )
    return agent, Session(authenticated_user_id=user), mock


def results(session: Session) -> list[Message]:
    return [m for m in session.messages if m.role == "tool"]


def decoded(message: Message) -> Any:
    return json.loads(
        json.loads(message.content.removeprefix("<tool_result>").removesuffix("</tool_result>"))
    )


def test_single_action_zero_extra_turn_read_survival_and_consumption(shop: Services) -> None:
    request = address_action(shop)
    agent, session, mock = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="将O1006改至公司88号，确认整份方案吗？"),
            using(call("get_order", order_id="O1006")),
            using(as_call(request)),
            LLMResponse(text="地址已修改"),
        ],
    )
    before = dump_hashes(shop.db)
    turn = agent.respond(session, "改地址")
    p = session.proposals[-1]
    assert turn.steps == 2 and len(mock.requests) == 2
    assert dump_hashes(shop.db) == before and p.status == "pending"
    assert (p.proposal_id, p.version, p.session_id, p.user_id) == (
        "P0001",
        1,
        session.session_id,
        "U006",
    )
    assert p.created_at == shop.clock.now().isoformat()
    assert p.actions[0].preview_amount_cents is None
    assert decoded(results(session)[0])["proposal"]["proposal_id"] == "P0001"
    agent.respond(session, "好的，确认，就这么办")
    assert p.status == "consumed" and p.actions[0].status == "executed"
    assert p.confirm_source == "natural_language"
    assert not any(m.is_error for m in results(session))
    assert shop.get_order("O1006").address.detail == "公司88号"


@pytest.mark.parametrize("reverse", [False, True])
def test_compound_independent_actions_in_either_order(shop: Services, reverse: bool) -> None:
    # O1006 totals: 13000*2 + 14000 = 40000; discount=2000; first paid share=24700.
    # Price protection: (13000-10000)*2*24700/26000 = 5700 cents exactly.
    requests = [
        address_action(shop),
        action("apply_price_protection", order_id="O1006", order_item_id="OI00008"),
    ]
    requests[1]["arguments"]["order_item_id"] = shop.order_details("O1006").items[0].id
    execution = list(reversed(requests)) if reverse else requests
    agent, session, _ = setup_agent(
        shop,
        [
            propose(*requests),
            LLMResponse(text="改址并原路退差¥57.00，请确认整份方案。"),
            using(as_call(execution[0])),
            using(call("get_order", order_id="O1006")),
            using(as_call(execution[1])),
            LLMResponse(text="改址完成，已退¥57.00"),
        ],
    )
    agent.respond(session, "改址和价保")
    assert session.proposals[0].actions[1].preview_amount_cents == 5700
    agent.respond(session, "确认")
    assert session.proposals[0].status == "consumed"
    assert [a.status for a in session.proposals[0].actions] == ["executed", "executed"]
    assert shop.get_order("O1006").price_protected
    assert shop.refunded_cents("O1006") == 5700
    assert not any(m.is_error for m in results(session))


@pytest.mark.parametrize(
    "tool, reply",
    [
        ("modify_address", "订单 O1005 改送公司88号，请确认。"),
        ("apply_price_protection", "订单 O1006 预计退差 ¥100.00，请确认。"),
        ("modify_address", "订单 O1006 改址并退差 ¥50.00，请确认。"),
        ("apply_price_protection", "订单 O1006 预计退款 ¥57.00，实际到账 100元，请确认。"),
    ],
)
def test_mismatched_presentation_blocks_natural_confirmation(
    shop: Services, tool: str, reply: str
) -> None:
    # O1006: price difference 3000 * 2 * (24700 / 26000) = 5700 cents.
    request = (
        address_action(shop)
        if tool == "modify_address"
        else action(tool, order_id="O1006", order_item_id=shop.order_details("O1006").items[0].id)
    )
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text=reply),
            using(as_call(request)),
            LLMResponse(text="需要重新说明方案"),
        ],
    )
    before = dump_hashes(shop.db)
    agent.respond(session, "请生成方案")
    proposal = session.proposals[-1]
    assert proposal.actions[0].preview_amount_cents == (None if tool == "modify_address" else 5700)
    assert session.presented_proposal_version is None
    agent.respond(session, "确认")
    assert "[POLICY_VIOLATION P02]" in results(session)[-1].content
    assert proposal.status == "pending" and proposal.confirm_source is None
    assert dump_hashes(shop.db) == before


def test_presentation_accepts_action_amounts_and_total(shop: Services) -> None:
    # Reorder one S0001 at 10000 cents; O1006 price protection is 5700 cents.
    price = action("apply_price_protection", order_id="O1006", order_item_id="OI00006")
    buy = action(
        "create_order",
        user_id="U006",
        items=[{"sku_id": "S0001", "qty": 1}],
        payment_method_id="PM0006",
    )
    agent, session, _ = setup_agent(shop, [propose(buy, price), LLMResponse(text="确认方案？")])
    agent.respond(session, "重新下单并申请价保")
    proposal = session.proposals[-1]
    assert presentation_matches("O1006：¥ 57、￥100.00，合计 157.00 元。", proposal)
    assert not presentation_matches("O1006：合计 157.001 元。", proposal)
    assert not presentation_matches("O1006：退 ¥57，另有 O1005。", proposal)


def test_stale_presentation_marker_cannot_authorize_but_web_button_can(shop: Services) -> None:
    request = address_action(shop)
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="订单 O1006 改送公司88号，请确认。"),
            LLMResponse(text="更正，订单 O1005 改送公司88号，请确认。"),
            using(as_call(request)),
            LLMResponse(text="需要重新说明方案"),
            using(as_call(request)),
            LLMResponse(text="已改址"),
        ],
    )
    agent.respond(session, "请生成方案")
    proposal = session.proposals[-1]
    assert session.presented_proposal_version == proposal.version
    agent.respond(session, "请复述一下")
    assert session.presented_proposal_version is None

    # A session saved before grounding may still have an unchecked presentation marker.
    session.presented_proposal_version = proposal.version
    before = dump_hashes(shop.db)
    agent.respond(session, "确认")
    assert "[POLICY_VIOLATION P02]" in results(session)[-1].content
    assert dump_hashes(shop.db) == before

    decide_proposal(session, proposal.proposal_id, proposal.version, "approve")
    agent.respond(session, "请执行")
    assert proposal.confirm_source == "web_button" and proposal.status == "consumed"
    assert shop.get_order("O1006").address.detail == "公司88号"


@pytest.mark.parametrize(
    "field", ["order_id", "receiver_name", "phone", "province", "city", "district", "detail"]
)
def test_structured_address_and_order_tampering_is_p02(shop: Services, field: str) -> None:
    other = shop.create_order("U006", [{"sku_id": "S0001", "qty": 1}], "PM0006")
    request = address_action(shop)
    tampered = as_call(request).model_copy(deep=True)
    if field == "order_id":
        tampered.arguments[field] = other.order_id
    else:
        tampered.arguments["address"][field] = "篡改值"
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="确认改址方案？"),
            using(tampered),
            LLMResponse(text="需重新确认"),
        ],
    )
    agent.respond(session, "改址")
    before = dump_hashes(shop.db)
    agent.respond(session, "确认")
    assert "[POLICY_VIOLATION P02]" in results(session)[-1].content
    assert "P0001 v1" in results(session)[-1].content
    assert session.proposals[0].actions[0].status == "approved"
    assert dump_hashes(shop.db) == before


@pytest.mark.parametrize("kind", ["return_qty", "exchange_sku"])
def test_return_quantity_and_exchange_sku_tampering(shop: Services, kind: str) -> None:
    # O1005: 3 units * 15000 paid = 45000; returning 1 -> 15000, 2 -> 30000 (also P08).
    # O1002: paid 13000; S0002=11000 -> 2000 refund; S0003=12000 -> 1000 refund.
    if kind == "return_qty":
        request = action(
            "request_return",
            order_id="O1005",
            items=[{"order_item_id": "OI00005", "qty": 1}],
            reason="退一件",
        )
        user = "U005"
    else:
        request = action(
            "request_exchange",
            order_id="O1002",
            order_item_id="OI00002",
            new_sku_id="S0002",
            reason="换颜色",
        )
        user = "U002"
    changed = as_call(request).model_copy(deep=True)
    if kind == "return_qty":
        changed.arguments["items"][0]["qty"] = 2
    else:
        changed.arguments["new_sku_id"] = "S0003"
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="请确认方案"),
            using(changed),
            LLMResponse(text="拒绝"),
        ],
        user=user,
    )
    agent.respond(session, "退换")
    before = dump_hashes(shop.db)
    agent.respond(session, "确认")
    assert "[POLICY_VIOLATION P02]" in results(session)[-1].content
    assert dump_hashes(shop.db) == before


@pytest.mark.parametrize("extra", ["cancel_order", "request_return"])
def test_undeclared_write_is_denied_even_in_the_same_batch(shop: Services, extra: str) -> None:
    request = address_action(shop)
    args = {"order_id": "O1006", "reason": "夹带操作"}
    if extra == "request_return":
        args["items"] = [{"order_item_id": shop.order_details("O1006").items[0].id, "qty": 1}]
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="确认改址？"),
            using(as_call(request), call(extra, **args)),
            LLMResponse(text="仅改址完成"),
        ],
    )
    agent.respond(session, "改址")
    agent.respond(session, "确认")
    assert [m.is_error for m in results(session)] == [False, False, True]
    assert "[POLICY_VIOLATION P02]" in results(session)[-1].content
    assert shop.get_order("O1006").status == "pending_shipment"
    assert shop.refunded_cents("O1006") == 0


def test_partial_consent_v2_and_deleted_v1_action_replay(shop: Services) -> None:
    # Move the seeded returnable order to the same test customer; one unit refunds 15000.
    shop.db.execute("UPDATE orders SET user_id='U006', payment_method_id='PM0006' WHERE id='O1005'")
    addr = address_action(shop)
    ret = action(
        "request_return",
        order_id="O1005",
        items=[{"order_item_id": "OI00005", "qty": 1}],
        reason="退一件",
    )
    agent, session, _ = setup_agent(
        shop,
        [
            propose(addr, ret),
            LLMResponse(text="改址并退一件¥150，请确认整份方案"),
            using(as_call(addr)),
            propose(addr),
            LLMResponse(text="新方案仅改址，确认吗？"),
            using(as_call(ret)),
            using(as_call(addr)),
            LLMResponse(text="改址已完成"),
        ],
    )
    agent.respond(session, "改址并退货")
    before = dump_hashes(shop.db)
    agent.respond(session, "只改地址，先不退货")
    v1, v2 = session.proposals
    assert (v1.status, v2.status, v2.version, v2.proposal_id) == (
        "superseded",
        "pending",
        2,
        "P0002",
    )
    assert all(a.status == "invalidated" for a in v1.actions)
    assert "P02" in results(session)[1].content and dump_hashes(shop.db) == before
    agent.respond(session, "确认")
    assert "[POLICY_VIOLATION P02]" in results(session)[3].content
    assert v2.status == "consumed"
    assert shop.refunded_cents("O1005") == 0


@pytest.mark.parametrize(
    "text",
    [
        "确认，只改地址",
        "好的，但退差成功才改地址",
        "确认，如果能当天到才改",
        "确认，地址改成另一个",
        "不取消订单，只改地址",
        "yes, only change address",
        "确认，但请等等",
        "好的，地址改到广州市",
        "确认改地址",
        "好的，退两件",
        "同意，数量为2",
        "确认，地址上海市示例路99号",
    ],
)
def test_partial_conditional_and_changed_consent_cannot_authorize(
    shop: Services, text: str
) -> None:
    request = address_action(shop)
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="确认方案？"),
            using(as_call(request)),
            LLMResponse(text="请重新确认"),
        ],
    )
    agent.respond(session, "改址")
    before = dump_hashes(shop.db)
    agent.respond(session, text)
    assert "[POLICY_VIOLATION P02]" in results(session)[-1].content
    assert dump_hashes(shop.db) == before


def test_bound_exclusion_revokes_cancel_despite_legacy_boundary_exemption(shop: Services) -> None:
    request = action("cancel_order", order_id="O1006", reason="取消")
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="确认取消？"),
            using(as_call(request)),
            LLMResponse(text="已拒绝"),
        ],
    )
    agent.respond(session, "取消")
    before = dump_hashes(shop.db)
    agent.respond(session, "确认，不取消订单")
    assert session.proposals[0].status == "rejected"
    assert "P02" in results(session)[-1].content and dump_hashes(shop.db) == before


def test_single_use_replay_after_success_is_denied(shop: Services) -> None:
    request = address_action(shop)
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="确认？"),
            using(as_call(request)),
            using(call("get_order", order_id="O1006")),
            using(as_call(request)),
            LLMResponse(text="完成"),
        ],
    )
    agent.respond(session, "改址")
    agent.respond(session, "确认")
    assert [m.is_error for m in results(session)] == [False, False, False, True]
    assert "[POLICY_VIOLATION P02]" in results(session)[-1].content
    assert session.proposals[0].status == "consumed"


@pytest.mark.parametrize(
    "mutation,rule",
    [
        ("order_status", "P03"),
        ("price_protected", "P04"),
        ("stock_insufficient", "P09"),
        ("stock_changed", "P02"),
        ("price_changed", "P02"),
    ],
)
def test_business_preconditions_rechecked_after_approval(
    shop: Services, mutation: str, rule: str
) -> None:
    if mutation == "order_status":
        request, user = address_action(shop), "U006"
    elif mutation == "price_protected":
        request, user = (
            action(
                "apply_price_protection",
                order_id="O1006",
                order_item_id=shop.order_details("O1006").items[0].id,
            ),
            "U006",
        )
    else:
        request, user = (
            action(
                "request_exchange",
                order_id="O1002",
                order_item_id="OI00002",
                new_sku_id="S0002",
                reason="换货",
            ),
            "U002",
        )
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="确认？"),
            using(as_call(request)),
            LLMResponse(text="原方案已失效"),
        ],
        user=user,
    )
    agent.respond(session, "办理")
    decide_proposal(session, "P0001", 1, "approve")
    updates = {
        "order_status": "UPDATE orders SET status='shipped' WHERE id='O1006'",
        "price_protected": "UPDATE orders SET price_protected=1 WHERE id='O1006'",
        "stock_insufficient": "UPDATE skus SET stock=0 WHERE id='S0002'",
        "stock_changed": "UPDATE skus SET stock=stock-1 WHERE id='S0002'",
        "price_changed": "UPDATE skus SET price_cents=10500 WHERE id='S0002'",
    }
    shop.db.execute(updates[mutation])
    before = dump_hashes(shop.db)
    agent.respond(session, "执行已批准方案")
    assert f"[POLICY_VIOLATION {rule}]" in results(session)[-1].content
    assert session.proposals[0].status == "expired"
    assert session.proposals[0].actions[0].status == "invalidated"
    assert dump_hashes(shop.db) == before


@pytest.mark.parametrize("guard", [True, False])
@pytest.mark.parametrize("user", [None, "U001"])
def test_proposal_p01_for_unauthenticated_and_other_customer(
    shop: Services, user: str | None, guard: bool
) -> None:
    request = address_action(shop)
    agent, session, _ = setup_agent(
        shop,
        [propose(request), LLMResponse(text="身份不符")],
        user=user,
        config=AgentConfig(proposal_gate=True, guard=guard),
    )
    before = dump_hashes(shop.db)
    agent.respond(session, "提案")
    assert "[POLICY_VIOLATION P01]" in results(session)[0].content
    assert session.proposals == [] and dump_hashes(shop.db) == before


def test_auth_switch_and_switch_back_immediately_expire_authorization(shop: Services) -> None:
    request = address_action(shop)
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="确认？"),
            using(as_call(request)),
            LLMResponse(text="请重新提案"),
        ],
    )
    agent.respond(session, "改址")
    decide_proposal(session, "P0001", 1, "approve")
    session.authenticated_user_id = "U001"
    assert session.proposals[0].status == "expired"
    session.authenticated_user_id = "U006"
    agent.respond(session, "确认")
    assert "[POLICY_VIOLATION P02]" in results(session)[-1].content


def test_large_refund_proposes_p08_and_hands_off_to_approval(shop: Services) -> None:
    # O1005 paid 45000 for 3 units; complete return refunds 45000 > 20000, requiring P08.
    request = action(
        "request_return",
        order_id="O1005",
        items=[{"order_item_id": "OI00005", "qty": 3}],
        reason="全部退货",
    )
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="¥450需人工审批，是否同意提交？"),
            using(as_call(request)),
            using(
                call(
                    "submit_approval",
                    order_id="O1005",
                    action="request_return",
                    args=request["arguments"],
                    reason="大额审批",
                )
            ),
            LLMResponse(text="已提交，待审批"),
        ],
        user="U005",
    )
    before = dump_hashes(shop.db)
    agent.respond(session, "退三件")
    p = session.proposals[0]
    assert p.actions[0].requires_approval and p.actions[0].preview_amount_cents == 45000
    assert "submit_approval" in decoded(results(session)[0])["guidance"][-1]
    assert dump_hashes(shop.db) == before
    agent.respond(session, "确认")
    assert "[POLICY_VIOLATION P08]" in results(session)[1].content
    assert not results(session)[2].is_error
    assert shop.refunded_cents("O1005") == 0
    assert (
        shop.db.execute("SELECT status FROM approvals WHERE order_id='O1005'").fetchone()[0]
        == "pending"
    )


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_web_button_uses_same_whole_plan_state_machine(shop: Services, decision: str) -> None:
    request = address_action(shop)
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="确认？"),
            using(as_call(request)),
            LLMResponse(text="结果"),
        ],
    )
    agent.respond(session, "改址")
    result = decide_proposal(session, "P0001", 1, decision)
    assert result.confirm_source == "web_button"
    result.status = "expired"  # The returned card must not be a mutable alias of authorization.
    agent.respond(session, "按钮已提交")
    p = session.proposals[0]
    assert p.status == ("consumed" if decision == "approve" else "rejected")
    assert results(session)[-1].is_error == (decision == "reject")
    with pytest.raises(PolicyError):
        decide_proposal(session, "P0001", 1, "approve")


@pytest.mark.parametrize("proposal_id,version", [("P0001", 2), ("P9999", 1), ("P0001", True)])
def test_web_button_mismatched_version_or_id_fails_closed(
    shop: Services, proposal_id: str, version: int
) -> None:
    agent, session, _ = setup_agent(
        shop, [propose(address_action(shop)), LLMResponse(text="确认？")]
    )
    agent.respond(session, "改址")
    with pytest.raises(PolicyError, match="P02"):
        decide_proposal(session, proposal_id, version, "approve")
    assert session.proposals[0].status == "pending"


def test_dependent_cancel_restore_coupon_then_purchase_requires_new_consent(shop: Services) -> None:
    # Cancel O1006 refunds 40000-2000=38000; cancellation is exempt from P08.
    # New purchase uses 2*10000-2000 coupon=18000 cents, only after coupon restoration.
    coupon = shop.get_order("O1006").user_coupon_id
    cancel = action("cancel_order", order_id="O1006", reason="重新下单")
    buy = action(
        "create_order",
        user_id="U006",
        items=[{"sku_id": "S0001", "qty": 2}],
        payment_method_id="PM0006",
        user_coupon_id=coupon,
    )
    agent, session, _ = setup_agent(
        shop,
        [
            propose(cancel, buy),
            propose(cancel),
            LLMResponse(text="先取消，确认吗？"),
            using(as_call(cancel)),
            propose(buy),
            using(as_call(buy)),
            LLMResponse(text="新订单实付¥180，是否确认？"),
            using(as_call(buy)),
            LLMResponse(text="已下单"),
        ],
    )
    agent.respond(session, "退券重拍")
    assert "P13" in results(session)[0].content and "分步提案" in results(session)[0].content
    assert len(session.proposals) == 1 and not session.proposals[0].actions[0].requires_approval
    agent.respond(session, "确认")
    assert len(session.proposals) == 2 and session.proposals[0].status == "consumed"
    assert session.proposals[1].actions[0].preview_amount_cents == 18000
    assert "P02" in results(session)[-1].content
    agent.respond(session, "确认")
    assert session.proposals[1].status == "consumed"


@pytest.mark.parametrize(
    "invalid",
    [
        action(
            "request_return",
            order_id="O1005",
            items=[{"order_item_id": "OI00005", "qty": "1"}],
            reason="退货",
        ),
        action("cancel_order", order_id="O1006", reason=" "),
        action("cancel_order", order_id="O1006", reason="取消", amount_cents=100),
    ],
)
def test_invalid_write_schema_never_creates_pending_proposal(
    shop: Services, invalid: dict[str, Any]
) -> None:
    agent, session, _ = setup_agent(
        shop,
        [propose(invalid), LLMResponse(text="参数错误")],
        user="U005" if invalid["tool_name"] == "request_return" else "U006",
    )
    before = dump_hashes(shop.db)
    agent.respond(session, "提案")
    assert results(session)[0].is_error and not session.proposals
    assert dump_hashes(shop.db) == before


def test_reason_is_nonempty_but_not_bound_word_for_word(shop: Services) -> None:
    request = action("cancel_order", order_id="O1006", reason="用户不要了")
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="确认取消并退¥380？"),
            using(call("cancel_order", order_id="O1006", reason=" ")),
            using(call("cancel_order", order_id="O1006", reason="买家要求取消")),
            LLMResponse(text="已取消"),
        ],
    )
    agent.respond(session, "取消")
    agent.respond(session, "确认")
    assert results(session)[1].is_error and "P02" in results(session)[1].content
    assert not results(session)[2].is_error and session.proposals[0].status == "consumed"


def test_unproposed_write_and_in_turn_confirmation_cannot_bypass_gate(shop: Services) -> None:
    request = address_action(shop)
    agent, session, _ = setup_agent(
        shop,
        [
            using(as_call(request)),
            propose(request),
            using(as_call(request)),
            LLMResponse(text="请确认"),
        ],
    )
    before = dump_hashes(shop.db)
    agent.respond(session, "确认，直接执行")
    assert [m.is_error for m in results(session)] == [True, False, True]
    assert all("P02" in m.content for m in (results(session)[0], results(session)[2]))
    assert session.proposals[0].status == "pending" and dump_hashes(shop.db) == before


def test_proposal_result_survives_context_projection(shop: Services) -> None:
    agent, session, _ = setup_agent(
        shop, [propose(address_action(shop)), LLMResponse(text="确认？")]
    )
    agent.respond(session, "改址")
    session.messages += [
        Message(role="user", content="查询"),
        Message(role="assistant", tool_calls=[call("get_order", order_id="O1006")]),
        Message(role="tool", tool_call_id="get_order", content="查询数据" * 1000),
        Message(role="user", content="后续"),
        Message(role="user", content="当前"),
    ]
    original = session.model_dump()
    view = prepare_messages(session.messages, agent.registry, message_limit=1)
    proposal_index = next(
        i
        for i, m in enumerate(session.messages)
        if m.role == "tool" and m.tool_call_id == "propose_actions"
    )
    assert proposal_index not in view.compressed_indices
    assert view.messages[proposal_index] == session.messages[proposal_index]
    assert view.compressed_indices and session.model_dump() == original


@pytest.mark.parametrize("mode", ["heuristic", "llm"])
def test_legacy_mode_keeps_text_batch_contract(shop: Services, mode: str) -> None:
    request = address_action(shop)
    config = AgentConfig(confirm_mode=mode)
    classifier = MockLLM([LLMResponse(text="YES")])
    agent, session, mock = setup_agent(
        shop,
        [LLMResponse(text="确认改址？"), using(as_call(request)), LLMResponse(text="完成")],
        config=config,
        classifier=classifier,
    )
    agent.respond(session, "改址")
    agent.respond(session, "确认")
    assert not results(session)[0].is_error and not session.proposals
    assert agent.registry.get("propose_actions") is None
    assert mock.requests[0].system == build_system(shop.clock)
    assert len(classifier.requests) == (1 if mode == "llm" else 0)


@pytest.mark.parametrize("mode", ["full", "on_demand"])
def test_proposal_prompt_replaces_legacy_batch_and_is_feature_gated(
    shop: Services, mode: str
) -> None:
    config = AgentConfig(proposal_gate=True, policy_mode=mode)
    agent, session, mock = setup_agent(shop, [LLMResponse(text="请提供资料")], config=config)
    agent.respond(session, "你好")
    assert "propose_actions" in mock.requests[0].system and "version+1" in mock.requests[0].system
    assert "一次同意只用于同一次模型响应" not in mock.requests[0].system
    assert "一次确认仅用于同一次模型响应" not in mock.requests[0].system
    policy = agent.registry.execute(
        "lookup_policy", {"topic": "confirm"}, ToolContext(session, config, shop.db, shop.clock)
    )
    assert (
        "一次确认仅用于同一次模型响应" not in policy.content and "propose_actions" in policy.content
    )
    config.confirm_gate = False
    mock.script.append(LLMResponse(text="你好"))
    agent.respond(session, "你好")
    assert mock.requests[-1].system == build_system(shop.clock, mode)


def test_trace_links_denials_and_allowed_execution_to_proposal(
    shop: Services, tmp_path: Path
) -> None:
    writer = TraceWriter(shop.clock, tmp_path)
    request = address_action(shop)
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="确认？"),
            using(call("cancel_order", order_id="O1006", reason="夹带")),
            using(as_call(request)),
            LLMResponse(text="完成"),
        ],
        trace=writer,
    )
    agent.respond(session, "改址")
    agent.respond(session, "确认")
    writer.close()
    verdicts = [e.data for e in read_trace(writer.path) if e.event == "confirm_verdict"]
    assert [v["allowed"] for v in verdicts] == [False, True]
    assert verdicts[0]["rule_ids"] == ["P02"]
    assert verdicts[1]["proposal_id"] == "P0001" and verdicts[1]["action_id"] == "ACT01"
    assert verdicts[1]["confirm_source"] == "natural_language"


@pytest.mark.parametrize(
    "kind,amount,status",
    [
        ("return", 15000, "completed"),
        ("exchange", 2000, "completed"),
        ("top_up", 10000, "awaiting_payment"),
    ],
)
def test_return_exchange_and_top_up_complete_with_service_preview(
    shop: Services, kind: str, amount: int, status: str
) -> None:
    # Return: 45000/3=15000. Exchange O1002: 13000-11000=2000 refund.
    # Top-up: target price 23000 - original 13000 = 10000; no refund approval applies.
    if kind == "return":
        request = action(
            "request_return",
            order_id="O1005",
            items=[{"order_item_id": "OI00005", "qty": 1}],
            reason="退货",
        )
        user = "U005"
    else:
        if kind == "top_up":
            shop.db.execute("UPDATE skus SET price_cents=23000 WHERE id='S0002'")
        request = action(
            "request_exchange",
            order_id="O1002",
            order_item_id="OI00002",
            new_sku_id="S0002",
            reason="换货",
        )
        user = "U002"
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="请确认方案金额"),
            using(as_call(request)),
            LLMResponse(text="已办理"),
        ],
        user=user,
    )
    agent.respond(session, "退换")
    assert session.proposals[0].actions[0].preview_amount_cents == amount
    agent.respond(session, "确认，按整份方案执行")
    assert session.proposals[0].status == "consumed"
    assert decoded(results(session)[-1])["status"] == status
    assert decoded(results(session)[-1])["amount_cents"] == amount


@pytest.mark.parametrize(
    "field", ["user_id", "items", "payment_method_id", "user_coupon_id", "address"]
)
def test_purchase_all_structured_fields_are_bound(shop: Services, field: str) -> None:
    # 2*10000=20000 cents; no coupon, even though unused UC0011 could discount it by 2000.
    request = action(
        "create_order",
        user_id="U006",
        items=[{"sku_id": "S0001", "qty": 2}],
        payment_method_id="PM0006",
    )
    changed = as_call(request).model_copy(deep=True)
    values = {
        "user_id": "U001",
        "items": [{"sku_id": "S0001", "qty": 1}],
        "payment_method_id": "PM0001",
        "user_coupon_id": "UC0011",
        "address": address_action(shop)["arguments"]["address"],
    }
    changed.arguments[field] = values[field]
    # Disable broad guard to prove the binding itself rejects every mismatched field.
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="实付¥200，确认吗？"),
            using(changed),
            LLMResponse(text="拒绝"),
        ],
        config=AgentConfig(proposal_gate=True, guard=False),
    )
    agent.respond(session, "购买")
    canonical = session.proposals[0].actions[0].canonical_args
    assert canonical["user_coupon_id"] is None and canonical["address"] is None
    before = dump_hashes(shop.db)
    agent.respond(session, "确认")
    assert "[POLICY_VIOLATION P02]" in results(session)[-1].content
    assert dump_hashes(shop.db) == before


@pytest.mark.parametrize("mutation", ["default_address", "coupon_status", "returned_qty"])
def test_implicit_address_coupon_and_return_balance_are_preconditions(
    shop: Services, mutation: str
) -> None:
    # Purchase: 2*10000 - coupon2000 = 18000. Return: 45000/3=15000 per unit.
    if mutation == "returned_qty":
        request = action(
            "request_return",
            order_id="O1005",
            items=[{"order_item_id": "OI00005", "qty": 1}],
            reason="退货",
        )
        user = "U005"
    else:
        request = action(
            "create_order",
            user_id="U006",
            items=[{"sku_id": "S0001", "qty": 2}],
            payment_method_id="PM0006",
            user_coupon_id="UC0011",
        )
        user = "U006"
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="确认？"),
            using(as_call(request)),
            LLMResponse(text="前提变化"),
        ],
        user=user,
    )
    agent.respond(session, "提案")
    decide_proposal(session, "P0001", 1, "approve")
    if mutation == "default_address":
        shop.db.execute(
            "UPDATE users SET default_address=? WHERE id='U006'",
            (json.dumps(address_action(shop)["arguments"]["address"]),),
        )
    elif mutation == "coupon_status":
        shop.db.execute("UPDATE user_coupons SET status='expired' WHERE id='UC0011'")
    else:
        shop.db.execute(
            "UPDATE order_items SET returned_qty=1, refunded_cents=15000 WHERE id='OI00005'"
        )
    before = dump_hashes(shop.db)
    agent.respond(session, "执行已批准方案")
    expected = "P13" if mutation == "coupon_status" else "P02"
    assert f"[POLICY_VIOLATION {expected}]" in results(session)[-1].content
    assert session.proposals[0].status == "expired" and dump_hashes(shop.db) == before


def test_superseding_partially_consumed_plan_preserves_executed_action(shop: Services) -> None:
    addr = address_action(shop)
    price = action(
        "apply_price_protection",
        order_id="O1006",
        order_item_id=shop.order_details("O1006").items[0].id,
    )
    agent, session, _ = setup_agent(
        shop,
        [
            propose(addr, price),
            LLMResponse(text="确认两项？"),
            using(as_call(addr)),
            LLMResponse(text="已改址，价保待处理"),
            propose(addr),
            LLMResponse(text="新方案仅改址，请确认"),
            using(as_call(price)),
            LLMResponse(text="旧动作拒绝"),
        ],
    )
    agent.respond(session, "改址价保")
    agent.respond(session, "确认")
    assert session.proposals[0].status == "approved"
    agent.respond(session, "重新提案")
    assert session.proposals[0].status == "superseded"
    assert [a.status for a in session.proposals[0].actions] == ["executed", "invalidated"]
    with pytest.raises(PolicyError):
        decide_proposal(session, "P0001", 1, "approve")
    agent.respond(session, "确认")
    assert "P02" in results(session)[-1].content
    assert not shop.get_order("O1006").price_protected


def test_service_failure_invalidates_unspent_authorization(
    shop: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = address_action(shop)
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="确认？"),
            using(as_call(request)),
            using(as_call(request)),
            LLMResponse(text="请重新提案"),
        ],
    )
    agent.respond(session, "改址")
    before = dump_hashes(shop.db)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(Services, "modify_address", fail)
    agent.respond(session, "确认")
    assert "TOOL_ERROR" in results(session)[1].content and "P02" in results(session)[2].content
    assert session.proposals[0].status == "expired"
    assert session.proposals[0].actions[0].status == "invalidated"
    assert dump_hashes(shop.db) == before


@pytest.mark.parametrize(
    "classification",
    [
        LLMResponse(text="YES"),
        LLMResponse(text="NO"),
        LLMResponse(text="YES", stop_reason="max_tokens"),
    ],
)
def test_proposal_llm_confirmation_still_fails_closed(
    shop: Services, classification: LLMResponse
) -> None:
    request = address_action(shop)
    classifier = MockLLM([classification])
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="确认？"),
            using(as_call(request)),
            LLMResponse(text="结果"),
        ],
        config=AgentConfig(proposal_gate=True, confirm_mode="llm"),
        classifier=classifier,
    )
    agent.respond(session, "改址")
    agent.respond(session, "确认")
    assert len(classifier.requests) == 1
    assert results(session)[-1].is_error == (
        classification.text != "YES" or classification.stop_reason != "end_turn"
    )


@pytest.mark.parametrize(
    "text",
    ["确认", "好的，确认，就这么办", "没问题", "确认，按整份方案执行", "确认，不取消其他订单"],
)
def test_whole_plan_confirmation_accepts_simple_and_safe_boundary_phrases(
    shop: Services, text: str
) -> None:
    request = address_action(shop)
    agent, session, _ = setup_agent(
        shop,
        [
            propose(request),
            LLMResponse(text="确认？"),
            using(as_call(request)),
            LLMResponse(text="已完成"),
        ],
    )
    agent.respond(session, "改址")
    agent.respond(session, text)
    assert session.proposals[0].status == "consumed" and not results(session)[-1].is_error


def test_existing_eval_task_supports_proposal_consent_and_split_execution(tmp_path: Path) -> None:
    # Frozen t019: (13000-10000)*2*24700/26000 = 5700 cents, unchanged gold/mentions.
    task = next(t for t in load_tasks("evals/tasks") if t.id == "t019_address_and_price")
    requests = [action(a.tool, **a.args) for a in task.gold_actions]
    mock = MockLLM(
        [
            using(
                call(
                    "find_user_by_phone", phone=task.user_profile.phone, name=task.user_profile.name
                )
            ),
            propose(*requests),
            LLMResponse(text="改址并退差57.00，请确认整份方案"),
            using(as_call(requests[0])),
            using(as_call(requests[1])),
            LLMResponse(text="改址完成，已退57.00"),
            LLMResponse(text="已完成"),
        ]
    )
    result = run_trial(task, mock, AgentConfig(proposal_gate=True), trace_dir=tmp_path)
    assert result.grade.passed, result.grade.model_dump()
    assert result.unconfirmed_writes == 0

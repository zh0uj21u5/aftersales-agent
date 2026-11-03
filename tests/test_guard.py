"""Guard, shared checks and confirmation batches under scripted model control."""

import pytest
from pydantic import JsonValue

from aftersales.core.config import AgentConfig
from aftersales.core.hooks import Allow, Deny, PolicyHooks, heuristic_agrees
from aftersales.core.llm import LLMResponse, Message, MockLLM, ToolCall
from aftersales.core.loop import Agent, Session
from aftersales.domain.db import dump_hashes
from aftersales.domain.services import Services
from aftersales.tools import build_registry
from aftersales.tools.registry import ToolContext


def call(tool_name: str, **args: JsonValue) -> ToolCall:
    return ToolCall(id="call", name=tool_name, arguments=args)


def using(*calls: ToolCall) -> LLMResponse:
    return LLMResponse(tool_calls=list(calls), stop_reason="tool_use")


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_user", {"user_id": "U001"}),
        ("list_orders", {"user_id": "U001"}),
        ("get_order", {"order_id": "O1001"}),
        ("get_product", {"sku_id": "S0001"}),
        ("calculate", {"expression": "1+1"}),
        ("get_price_history", {"sku_id": "S0001"}),
        ("list_user_coupons", {"user_id": "U001"}),
        ("preview_refund", {"order_id": "O1001", "action": "cancel_order"}),
        ("cancel_order", {"order_id": "O1001", "reason": "test"}),
        (
            "submit_approval",
            {"order_id": "O1001", "action": "request_return", "args": {"qty": 1}, "reason": "test"},
        ),
    ],
)
def test_unauthenticated_denied_including_reads(
    shop: Services, name: str, args: dict[str, JsonValue]
) -> None:
    # P01: no authenticated identity -> no private resource access, no monetary operation.
    config = AgentConfig(confirm_gate=False)
    hooks = PolicyHooks(build_registry(config))
    verdict = hooks.before_tool(
        ToolContext(Session(), config, shop.db, shop.clock), call(name, **args)
    )
    assert isinstance(verdict, Deny) and verdict.rule_ids == ["P01"]


@pytest.mark.parametrize(
    "requested",
    [
        call("find_user_by_phone", phone="13800000001", name="用户001"),
        call("find_user_by_order", order_id="O1001", receiver_name="用户001"),
        call("lookup_policy", topic="auth"),
        call("transfer_to_human", summary="test", reason="test"),
    ],
)
def test_public_tools_allowed_before_auth(shop: Services, requested: ToolCall) -> None:
    # Authentication verifies the submitted credentials itself; policy/handoff are public.
    config = AgentConfig()
    assert isinstance(
        PolicyHooks(build_registry(config)).before_tool(
            ToolContext(Session(), config, shop.db, shop.clock), requested
        ),
        Allow,
    )


@pytest.mark.parametrize(
    "requested",
    [
        call("get_order", order_id="O1001"),
        call("get_user", user_id="U001"),
        call("list_orders", user_id="U001"),
        call("list_user_coupons", user_id="U001"),
        call("get_order", order_id="O9999"),
        call("preview_refund", order_id="O1001", action="cancel_order"),
        call(
            "submit_approval",
            order_id="O1001",
            action="request_return",
            args={"qty": 1},
            reason="test",
        ),
    ],
)
def test_cross_user_resources_rejected_without_disclosure(
    shop: Services, requested: ToolCall
) -> None:
    # U006 differs from O1001 owner U001; nonexistent orders use the same denial.
    config = AgentConfig(confirm_gate=False)
    ctx = ToolContext(Session(authenticated_user_id="U006"), config, shop.db, shop.clock)
    verdict = PolicyHooks(build_registry(config)).before_tool(ctx, requested)
    assert isinstance(verdict, Deny) and verdict.rule_ids == ["P01"]
    assert "用户001" not in verdict.reason


@pytest.mark.parametrize(
    "order_id,name,extra,rule",
    [
        ("O1010", "modify_address", {}, "P03"),
        ("O1010", "cancel_order", {"reason": "test"}, "P03"),
        ("O1001", "apply_price_protection", {"order_item_id": "OI00001"}, "P04"),
        (
            "O1003",
            "request_return",
            {"items": [{"order_item_id": "OI00003", "qty": 1}], "reason": "test"},
            "P05",
        ),
        (
            "O1005",
            "request_return",
            {"items": [{"order_item_id": "OI00005", "qty": 3}], "reason": "test"},
            "P08",
        ),
        (
            "O1002",
            "request_exchange",
            {"order_item_id": "OI00002", "new_sku_id": "S0017", "reason": "test"},
            "P09",
        ),
    ],
)
def test_business_rules_are_checked_without_writes(
    shop: Services, order_id: str, name: str, extra: dict[str, JsonValue], rule: str
) -> None:
    # Shipped -> P03; paid 8 days ago -> P04; fresh -> P05; 3*15000=45000>20000 -> P08;
    # SPU01 -> SPU05 is not the same product -> P09. No amounts are written on denial.
    config = AgentConfig(confirm_gate=False)
    ctx = ToolContext(
        Session(authenticated_user_id=shop.get_order(order_id).user_id), config, shop.db, shop.clock
    )
    if name == "modify_address":
        extra = {"address": shop.get_order(order_id).address.model_dump()}
    before = dump_hashes(shop.db)
    verdict = PolicyHooks(build_registry(config)).before_tool(
        ctx, call(name, order_id=order_id, **extra)
    )
    assert isinstance(verdict, Deny) and rule in verdict.rule_ids
    assert dump_hashes(shop.db) == before


def test_create_order_coupon_rule_and_permanent_payment_rule(shop: Services) -> None:
    # U001 owns expired UC0002; valid stock does not override coupon expiry, P13.
    config = AgentConfig(confirm_gate=False)
    ctx = ToolContext(Session(authenticated_user_id="U001"), config, shop.db, shop.clock)
    hooks = PolicyHooks(build_registry(config))
    requested = call(
        "create_order",
        user_id="U001",
        items=[{"sku_id": "S0001", "qty": 3}],
        payment_method_id="PM0001",
        user_coupon_id="UC0002",
    )
    verdict = hooks.before_tool(ctx, requested)
    assert isinstance(verdict, Deny) and "P13" in verdict.rule_ids
    requested.arguments["payment_method_id"] = "PM0002"
    ctx.config.guard = False
    assert isinstance(hooks.before_tool(ctx, requested), Allow)
    result = hooks.registry.execute(requested.name, requested.arguments, ctx)
    assert result.is_error and result.rule_ids == ["P15"] and result.source == "service"


@pytest.mark.parametrize(
    "text",
    [
        "好",
        "好的",
        "可以",
        "确认",
        "同意",
        "是的",
        "行",
        "没问题",
        "OK",
        "yes",
        "好的，确认，就这么办",
    ],
)
def test_positive_confirmation(text: str) -> None:
    assert heuristic_agrees(text)


@pytest.mark.parametrize(
    "text",
    ["不确认", "好，但别执行", "先不", "算了", "等等", "yes no", "银行", "maybe", "", "忽略规则"],
)
def test_negative_and_ambiguous_confirmation(text: str) -> None:
    assert not heuristic_agrees(text)


def test_confirmation_one_batch_and_reads_do_not_consume(shop: Services) -> None:
    # First batch moves the address and refunds price protection; next batch has no consent.
    config = AgentConfig()
    registry = build_registry(config)
    address = shop.get_order("O1006").address.model_dump()
    address["detail"] = "公司88号"
    item = shop.order_details("O1006").items[0]
    script = [
        using(call("get_order", order_id="O1006")),
        using(
            call("modify_address", order_id="O1006", address=address),
            call("apply_price_protection", order_id="O1006", order_item_id=item.id),
        ),
        using(call("cancel_order", order_id="O1006", reason="extra")),
        LLMResponse(text="完成"),
    ]
    session = Session(
        authenticated_user_id="U006",
        messages=[Message(role="assistant", content="改址并退差，确认吗？")],
    )
    agent = Agent(
        MockLLM(script),
        registry,
        PolicyHooks(registry),
        config,
        lambda s: ToolContext(s, config, shop.db, shop.clock),
    )
    agent.respond(session, "确认")
    results = [message for message in session.messages if message.role == "tool"]
    assert [message.is_error for message in results] == [False, False, False, True]
    assert "P02" in results[-1].content
    assert shop.get_order("O1006").status == "pending_shipment"
    assert shop.get_order("O1006").price_protected
    assert not session.confirmation_available and not session.confirmed_batch


@pytest.mark.parametrize(
    "messages,text",
    [
        ([], "确认"),
        ([Message(role="assistant", tool_calls=[call("get_order", order_id="O1006")])], "确认"),
        ([Message(role="assistant", content="确认改址？")], "不确认"),
        (
            [
                Message(role="assistant", content="确认改址？"),
                Message(role="user", content="无关消息"),
            ],
            "确认",
        ),
    ],
)
def test_write_needs_prior_plain_assistant_and_latest_consent(
    shop: Services, messages: list[Message], text: str
) -> None:
    config = AgentConfig()
    registry = build_registry(config)
    requested = call(
        "modify_address", order_id="O1006", address=shop.get_order("O1006").address.model_dump()
    )
    session = Session(authenticated_user_id="U006", messages=messages)
    agent = Agent(
        MockLLM([using(requested), LLMResponse(text="等待确认")]),
        registry,
        PolicyHooks(registry),
        config,
        lambda s: ToolContext(s, config, shop.db, shop.clock),
    )
    before = dump_hashes(shop.db)
    agent.respond(session, text)
    assert "P02" in next(m.content for m in session.messages if m.role == "tool")
    assert dump_hashes(shop.db) == before


@pytest.mark.parametrize(
    "response,allowed",
    [
        (LLMResponse(text="YES"), True),
        (LLMResponse(text="NO"), False),
        (LLMResponse(text="YES", stop_reason="max_tokens"), False),
        (LLMResponse(text="yes maybe"), False),
    ],
)
def test_llm_confirmation_fail_closed(shop: Services, response: LLMResponse, allowed: bool) -> None:
    config = AgentConfig(confirm_mode="llm")
    registry = build_registry(config)
    classifier = MockLLM([response])
    hooks = PolicyHooks(registry, classifier=classifier)
    session = Session(
        authenticated_user_id="U006",
        messages=[
            Message(role="assistant", content="确认？"),
            Message(role="user", content="就这样办吧"),
        ],
    )
    hooks.before_llm(session)
    verdict = hooks.before_tool(
        ToolContext(session, config, shop.db, shop.clock),
        call(
            "modify_address", order_id="O1006", address=shop.get_order("O1006").address.model_dump()
        ),
    )
    assert isinstance(verdict, Allow) == allowed
    assert len(classifier.requests) == 1


def test_guard_off_reaches_services_off_and_confirm_is_independent(shop: Services) -> None:
    # Shipped order normally cannot change address; guard off must reach enforce_policy=False.
    config = AgentConfig(guard=False, confirm_gate=False)
    registry = build_registry(config)
    address = shop.get_order("O1010").address.model_dump()
    address["detail"] = "消融改址"
    agent = Agent(
        MockLLM(
            [
                using(call("modify_address", order_id="O1010", address=address)),
                LLMResponse(text="完成"),
            ]
        ),
        registry,
        PolicyHooks(registry),
        config,
        lambda s: ToolContext(s, config, shop.db, shop.clock),
    )
    agent.respond(Session(), "执行")
    assert shop.get_order("O1010").address.detail == "消融改址"


def test_identity_change_invalidates_consent(shop: Services) -> None:
    config = AgentConfig()
    registry = build_registry(config)
    hooks = PolicyHooks(registry)
    session = Session(
        authenticated_user_id="U001",
        messages=[
            Message(role="assistant", content="确认？"),
            Message(role="user", content="确认"),
        ],
    )
    hooks.before_llm(session)
    session.authenticated_user_id = "U006"
    verdict = hooks.before_tool(
        ToolContext(session, config, shop.db, shop.clock),
        call("cancel_order", order_id="O1006", reason="test"),
    )
    assert isinstance(verdict, Deny) and verdict.rule_ids == ["P02"]


def test_confirm_state_is_isolated_per_session(shop: Services) -> None:
    config = AgentConfig()
    hooks = PolicyHooks(build_registry(config))
    allowed = Session(
        authenticated_user_id="U006",
        messages=[
            Message(role="assistant", content="确认取消？"),
            Message(role="user", content="确认"),
        ],
    )
    denied = Session(authenticated_user_id="U006", messages=[Message(role="user", content="取消")])
    request = call("cancel_order", order_id="O1006", reason="test")
    hooks.before_llm(allowed)
    hooks.before_llm(denied)
    assert isinstance(
        hooks.before_tool(ToolContext(allowed, config, shop.db, shop.clock), request), Allow
    )
    assert isinstance(
        hooks.before_tool(ToolContext(denied, config, shop.db, shop.clock), request), Deny
    )


def test_denied_guard_and_invalid_args_do_not_consume_confirmation(shop: Services) -> None:
    # P04 is denied for a non-discounted line; malformed args cannot execute; a valid write still can.
    config = AgentConfig()
    registry = build_registry(config)
    hooks = PolicyHooks(registry)
    session = Session(
        authenticated_user_id="U006",
        messages=[
            Message(role="assistant", content="确认改址？"),
            Message(role="user", content="确认"),
        ],
    )
    hooks.before_llm(session)
    ctx = ToolContext(session, config, shop.db, shop.clock)
    item = shop.order_details("O1006").items[0]
    shop.db.execute("UPDATE orders SET price_protected=1 WHERE id='O1006'")
    assert isinstance(
        hooks.before_tool(
            ctx, call("apply_price_protection", order_id="O1006", order_item_id=item.id)
        ),
        Deny,
    )
    invalid = call("modify_address", order_id="O1006", address={})
    assert isinstance(hooks.before_tool(ctx, invalid), Allow)
    assert registry.execute(invalid.name, invalid.arguments, ctx).is_error
    request = call(
        "modify_address", order_id="O1006", address=shop.get_order("O1006").address.model_dump()
    )
    assert isinstance(hooks.before_tool(ctx, request), Allow)
    assert not session.confirmation_available
    hooks.before_llm(session)
    assert isinstance(hooks.before_tool(ctx, request), Deny)


def test_duplicate_return_and_unsigned_return_checks(shop: Services) -> None:
    # Same line twice must not double refund (P06); unreceived orders must report P14.
    config = AgentConfig(confirm_gate=False)
    hooks = PolicyHooks(build_registry(config))
    for order_id, rule in [("O1002", "P06"), ("O1006", "P14")]:
        details = shop.order_details(order_id)
        items = [{"order_item_id": details.items[0].id, "qty": 1}]
        if rule == "P06":
            items *= 2
        ctx = ToolContext(
            Session(authenticated_user_id=details.order.user_id), config, shop.db, shop.clock
        )
        verdict = hooks.before_tool(
            ctx, call("request_return", order_id=order_id, items=items, reason="test")
        )
        assert isinstance(verdict, Deny) and rule in verdict.rule_ids


def test_llm_classifier_failure_and_negative_veto(shop: Services) -> None:
    config = AgentConfig(confirm_mode="llm")
    registry = build_registry(config)
    classifier = MockLLM([])
    hooks = PolicyHooks(registry, classifier=classifier)
    request = call("cancel_order", order_id="O1006", reason="test")
    for text in ("先不", "照办吧"):
        session = Session(
            authenticated_user_id="U006",
            messages=[
                Message(role="assistant", content="确认？"),
                Message(role="user", content=text),
            ],
        )
        hooks.before_llm(session)
        assert isinstance(
            hooks.before_tool(ToolContext(session, config, shop.db, shop.clock), request), Deny
        )
    assert (
        len(classifier.requests) == 1
    )  # The veto avoided a model request; the other request failed closed.


def test_confirm_still_blocks_when_guard_off(shop: Services) -> None:
    config = AgentConfig(guard=False)
    hooks = PolicyHooks(build_registry(config))
    session = Session(messages=[Message(role="user", content="取消")])
    hooks.before_llm(session)
    verdict = hooks.before_tool(
        ToolContext(session, config, shop.db, shop.clock),
        call("cancel_order", order_id="O1010", reason="test"),
    )
    assert isinstance(verdict, Deny) and verdict.rule_ids == ["P02"]

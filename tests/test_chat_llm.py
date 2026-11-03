"""Opt-in live M1 smoke test with a fresh disposable database."""

import io
from pathlib import Path

import pytest
from rich.console import Console

from aftersales.core.config import load_settings
from aftersales.core.llm import make_client
from aftersales.core.loop import Session
from aftersales.core.runtime import build_agent
from aftersales.core.tracing import TraceWriter, read_trace, render_trace
from aftersales.domain.services import Services


@pytest.mark.llm
def test_real_m1_auth_address_price_protection(shop: Services, tmp_path: Path) -> None:
    try:
        settings = load_settings()
    except ValueError:
        pytest.skip("请配置 .env 后运行真实模型验收")
    # 26000/14000 lines minus proportional 2000 coupon -> 24700/13300 paid shares.
    # First line: 3000 * 2 * 24700 / 26000 = 5700 cents, below P08's 20000 threshold.
    item = shop.order_details("O1006").items[0]
    expected = shop.preview_refund("O1006", "apply_price_protection", item.id).amount_cents
    writer = TraceWriter(shop.clock, tmp_path, "m1-live")
    try:
        agent = build_agent(shop.db, shop.clock, make_client(), settings.agent_config(), writer)
        session = Session()
        result = agent.respond(
            session,
            "我叫用户006，手机号13800000006。请处理我的O1006：把收货地址改为上海市上海市徐汇区公司路88号，"
            "收货人用户006、电话13800000006不变；再给订单中的S0001商品申请价保。"
            "先查清条件和退款金额，复述方案让我确认。不要取消订单。",
        )
        assert result.stop_reason == "end_turn", result.reply
        assert not shop.get_order("O1006").price_protected
        assert shop.get_order("O1006").address.detail != "公司路88号"
        for _ in range(4):
            result = agent.respond(session, "好的，确认按你刚才的方案执行。")
            assert result.stop_reason == "end_turn", result.reply
            order = shop.get_order("O1006")
            if order.price_protected and order.address.detail == "公司路88号":
                break
        assert shop.get_order("O1006").price_protected, result.reply
        assert shop.get_order("O1006").address.detail == "公司路88号", result.reply
        assert shop.get_order("O1006").status == "pending_shipment"
        assert shop.refunded_cents("O1006") == expected
    finally:
        writer.close()
    events = read_trace(writer.path)
    assert any(e.event == "confirm_verdict" and e.data["allowed"] for e in events)
    render_trace(writer.path, Console(file=io.StringIO()))

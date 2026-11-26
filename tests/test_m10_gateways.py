"""Verify the channel wire contract without reaching a network provider."""

import json

import httpx
import pytest

from aftersales.core.llm import run_sync
from aftersales.domain.clock import FixedClock
from aftersales.domain.gateways import HttpFulfillmentGateway, HttpPaymentGateway, RefundInstruction


def test_channel_wire_contract_uses_exact_refund_amount_and_execution_key() -> None:
    now = FixedClock().now()
    settlement = {
        "refund_id": "R001",
        "order_id": "O1006",
        "execution_key": "approval:AP001",
        "amount_cents": 25000,
        "status": "success",
    }

    def handle(request: httpx.Request) -> httpx.Response:
        match request.url.path:
            case "/refunds":
                item = json.loads(request.content)["refunds"][0]
                assert item["payment_method_id"] == "PM0006" and type(item["amount_cents"]) is int
                assert item["amount_cents"] == 25000
                assert request.headers["Idempotency-Key"] == "approval:AP001"
                return httpx.Response(200, json={"refunds": [settlement]})
            case "/refunds/by-reference":
                assert request.url.params["refund_id"] == "R001"
                return httpx.Response(404)
            case "/refunds/statement":
                assert request.url.params["as_of"] == now.isoformat()
                return httpx.Response(
                    200, json={"as_of": now.isoformat(), "complete": True, "refunds": [settlement]}
                )
            case "/intercepts":
                assert request.headers["Idempotency-Key"] == "approval:AP001"
                return httpx.Response(202)
            case _:
                pytest.fail(f"Unexpected request: {request.url.path}")

    async def check() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle),
            base_url="http://gateway.test",
        ) as client:
            payment = HttpPaymentGateway(client)
            # 25,000 cents from the original captured payment, with no conversions or rounding.
            result = await payment.refund(
                "approval:AP001",
                [
                    RefundInstruction(
                        refund_id="R001",
                        order_id="O1006",
                        amount_cents=25000,
                        payment_method_id="PM0006",
                    )
                ],
            )
            assert result[0].model_dump() == settlement
            assert await payment.lookup("R001") is None
            assert (await payment.statement(now)).refunds == result
            with pytest.raises(TimeoutError):
                await HttpFulfillmentGateway(client).intercept("approval:AP001", "O1006")

    run_sync(check())

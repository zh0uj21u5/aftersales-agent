"""Async payment and fulfillment contracts with injectable sandbox faults."""

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from aftersales.domain.clock import Clock
from aftersales.domain.models import Address


class RefundInstruction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    refund_id: str = Field(min_length=1, max_length=80)
    order_id: str = Field(min_length=1, max_length=80)
    amount_cents: int = Field(strict=True, gt=0)
    payment_method_id: str


class ChannelRefund(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    refund_id: str = Field(min_length=1, max_length=80)
    order_id: str = Field(min_length=1, max_length=80)
    execution_key: str = Field(min_length=1, max_length=128)
    amount_cents: int = Field(strict=True, gt=0)
    status: Literal["processing", "success", "failed"]


class PaymentStatement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: AwareDatetime
    complete: bool
    refunds: list[ChannelRefund]


class PaymentGateway(Protocol):
    async def refund(
        self, idempotency_key: str, refunds: Sequence[RefundInstruction]
    ) -> list[ChannelRefund]: ...

    async def lookup(self, refund_id: str) -> ChannelRefund | None: ...

    async def statement(self, as_of: datetime) -> PaymentStatement: ...


class FulfillmentGateway(Protocol):
    async def intercept(self, idempotency_key: str, order_id: str) -> None: ...

    async def change_address(
        self, idempotency_key: str, order_id: str, address: Address
    ) -> None: ...


class GatewayRejected(RuntimeError):
    """The channel rejected the envelope. An operator must review it."""


class HttpPaymentGateway:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def refund(
        self, idempotency_key: str, refunds: Sequence[RefundInstruction]
    ) -> list[ChannelRefund]:
        response = await self.client.post(
            "/refunds",
            headers={"Idempotency-Key": idempotency_key},
            json={"refunds": [item.model_dump(mode="json") for item in refunds]},
        )
        _check(response)
        return [ChannelRefund.model_validate(row) for row in response.json()["refunds"]]

    async def lookup(self, refund_id: str) -> ChannelRefund | None:
        response = await self.client.get("/refunds/by-reference", params={"refund_id": refund_id})
        if response.status_code == 404:
            return None
        _check(response)
        return ChannelRefund.model_validate(response.json())

    async def statement(self, as_of: datetime) -> PaymentStatement:
        response = await self.client.get("/refunds/statement", params={"as_of": as_of.isoformat()})
        _check(response)
        return PaymentStatement.model_validate(response.json())


class HttpFulfillmentGateway:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def intercept(self, idempotency_key: str, order_id: str) -> None:
        response = await self.client.post(
            "/intercepts",
            headers={"Idempotency-Key": idempotency_key},
            json={"order_id": order_id},
        )
        _check(response, terminal=True)

    async def change_address(self, idempotency_key: str, order_id: str, address: Address) -> None:
        response = await self.client.post(
            "/address-changes",
            headers={"Idempotency-Key": idempotency_key},
            json={"order_id": order_id, "address": address.model_dump(mode="json")},
        )
        _check(response, terminal=True)


def _check(response: httpx.Response, *, terminal: bool = False) -> None:
    if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
        raise GatewayRejected("渠道拒绝该请求")
    response.raise_for_status()
    if terminal and response.status_code == 202:
        raise TimeoutError("履约确认尚未完成")


class SandboxPayment:
    """Replay a whole envelope, including after a simulated lost response."""

    def __init__(
        self, clock: Clock, faults: Sequence[Literal["timeout", "lost_response", "failed"]] = ()
    ) -> None:
        self.clock = clock
        self.faults = list(faults)
        self.attempts: list[str] = []
        self.envelopes: dict[str, tuple[RefundInstruction, ...]] = {}
        self.refunds: dict[str, ChannelRefund] = {}
        self.settled_at: dict[str, datetime] = {}

    async def refund(
        self, idempotency_key: str, refunds: Sequence[RefundInstruction]
    ) -> list[ChannelRefund]:
        self.attempts.append(idempotency_key)
        fault = self.faults.pop(0) if self.faults else None
        if fault == "timeout":
            raise TimeoutError("sandbox timeout")
        if idempotency_key in self.envelopes and self.envelopes[idempotency_key] != tuple(refunds):
            raise GatewayRejected("sandbox idempotency payload conflict")
        self.envelopes[idempotency_key] = tuple(refunds)
        for refund in refunds:
            if refund.refund_id not in self.refunds:
                self.refunds[refund.refund_id] = ChannelRefund(
                    refund_id=refund.refund_id,
                    order_id=refund.order_id,
                    execution_key=idempotency_key,
                    amount_cents=refund.amount_cents,
                    status="failed" if fault == "failed" else "success",
                )
                self.settled_at[refund.refund_id] = self.clock.now()
        if fault == "lost_response":
            raise TimeoutError("sandbox accepted the envelope before timeout")
        return [self.refunds[item.refund_id] for item in refunds]

    async def lookup(self, refund_id: str) -> ChannelRefund | None:
        return self.refunds.get(refund_id)

    async def statement(self, as_of: datetime) -> PaymentStatement:
        return PaymentStatement(
            as_of=as_of,
            complete=True,
            refunds=[
                item
                for key, item in self.refunds.items()
                if item.status == "success" and self.settled_at.get(key, self.clock.now()) <= as_of
            ],
        )


class SandboxFulfillment:
    def __init__(self) -> None:
        self.commands: dict[str, tuple[str, Address | None]] = {}

    def _record(self, key: str, order_id: str, address: Address | None) -> None:
        payload = (order_id, address)
        if self.commands.setdefault(key, payload) != payload:
            raise GatewayRejected("sandbox idempotency payload conflict")

    async def intercept(self, idempotency_key: str, order_id: str) -> None:
        self._record(idempotency_key, order_id, None)

    async def change_address(self, idempotency_key: str, order_id: str, address: Address) -> None:
        self._record(idempotency_key, order_id, address)


@dataclass(frozen=True)
class GatewaySettings:
    payment_url: str | None = None
    fulfillment_url: str | None = None
    payment_token: str | None = field(default=None, repr=False)
    fulfillment_token: str | None = field(default=None, repr=False)
    sandbox: bool = False

    @classmethod
    def from_values(cls, values: Mapping[str, str | None]) -> "GatewaySettings":
        mode = values.get("GATEWAY_MODE") or "http"
        if mode not in {"http", "sandbox"}:
            raise ValueError("GATEWAY_MODE 只支持 http 或 sandbox")
        return cls(
            payment_url=values.get("PAYMENT_GATEWAY_URL"),
            fulfillment_url=values.get("FULFILLMENT_GATEWAY_URL"),
            payment_token=values.get("PAYMENT_GATEWAY_TOKEN"),
            fulfillment_token=values.get("FULFILLMENT_GATEWAY_TOKEN"),
            sandbox=mode == "sandbox",
        )


@asynccontextmanager
async def open_gateways(
    settings: GatewaySettings,
    clock: Clock,
) -> AsyncIterator[tuple[PaymentGateway | None, FulfillmentGateway | None]]:
    if settings.sandbox:
        yield SandboxPayment(clock), SandboxFulfillment()
        return
    async with AsyncExitStack() as stack:

        async def client(url: str, token: str | None) -> httpx.AsyncClient:
            return await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=url,
                    headers={"Authorization": f"Bearer {token}"} if token else {},
                    timeout=8,
                    follow_redirects=False,
                )
            )

        payment = (
            HttpPaymentGateway(await client(settings.payment_url, settings.payment_token))
            if settings.payment_url
            else None
        )
        fulfillment = (
            HttpFulfillmentGateway(
                await client(settings.fulfillment_url, settings.fulfillment_token)
            )
            if settings.fulfillment_url
            else None
        )
        yield payment, fulfillment

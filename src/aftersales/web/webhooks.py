"""Authenticate signed payment events and process each exact event only once."""

import hashlib
import hmac
import re
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, ValidationError

from aftersales.domain.clock import Clock
from aftersales.domain.db import transaction
from aftersales.domain.gateways import ChannelRefund
from aftersales.domain.settlement import apply_channel
from aftersales.domain.storage import PgConnection

if TYPE_CHECKING:
    from aftersales.web.production import Production

PAYMENT_WEBHOOK_PATH = "/api/webhooks/payment"
WINDOW_SECONDS = 300
MAX_BODY = 16_384
WebhookResult = Literal["applied", "duplicate", "unchanged", "mismatch", "conflict"]


class PaymentEvent(ChannelRefund):
    event_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


def sign_payment_event(secret: bytes, timestamp: str, body: bytes) -> str:
    return hmac.new(secret, timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def _record(db: PgConnection, clock: Clock, event: PaymentEvent, digest: str) -> WebhookResult:
    with transaction(db):
        refund = db.execute(
            """SELECT execution_key, order_id FROM refunds WHERE id=?
               AND execution_key IS NOT NULL""",
            (event.refund_id,),
        ).fetchone()
        if refund is None:
            raise KeyError(event.refund_id)
        if (refund[0], refund[1]) != (event.execution_key, event.order_id):
            return "mismatch"
        db.execute("SELECT id FROM orders WHERE id=? FOR UPDATE", (event.order_id,))
        inserted = db.execute(
            """INSERT INTO webhook_receipts (event_id, payload_hash, refund_id, received_at)
               VALUES (?, ?, ?, ?) ON CONFLICT (event_id) DO NOTHING""",
            (event.event_id, digest, event.refund_id, clock.now().isoformat()),
        ).rowcount
        if not inserted:
            original = db.execute(
                "SELECT payload_hash FROM webhook_receipts WHERE event_id=?",
                (event.event_id,),
            ).fetchone()
            return "duplicate" if hmac.compare_digest(original[0], digest) else "conflict"
        return apply_channel(db, clock, event)


def router(production: "Production") -> APIRouter:
    routes = APIRouter()

    @routes.post(PAYMENT_WEBHOOK_PATH)
    async def payment_webhook(request: Request) -> dict[str, str]:
        secret = production.settings.webhook_secret
        if secret is None:
            raise HTTPException(503, "支付回调尚未配置")
        timestamp = request.headers.get("X-Timestamp", "")
        signature = request.headers.get("X-Signature", "")
        if (
            len(request.headers.getlist("X-Timestamp")) != 1
            or len(request.headers.getlist("X-Signature")) != 1
            or not re.fullmatch(r"[0-9]{1,12}", timestamp)
            or not re.fullmatch(r"[0-9a-f]{64}", signature)
            or abs(production.clock.now().timestamp() - int(timestamp)) > WINDOW_SECONDS
        ):
            raise HTTPException(401, "支付回调鉴权失败")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_BODY:
                raise HTTPException(413, "支付回调请求体过长")
            body.extend(chunk)
        raw = bytes(body)
        if not hmac.compare_digest(signature, sign_payment_event(secret, timestamp, raw)):
            raise HTTPException(401, "支付回调鉴权失败")
        try:
            event = PaymentEvent.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(422, "支付回调格式无效") from None
        try:
            outcome = await production.storage.run(
                lambda db: _record(db, production.clock, event, hashlib.sha256(raw).hexdigest())
            )
        except KeyError:
            raise HTTPException(404, "退款记录不存在") from None
        if outcome in {"mismatch", "conflict"}:
            raise HTTPException(409, "支付回调与既有记录冲突")
        return {"status": outcome}

    return routes

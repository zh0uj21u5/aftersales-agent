"""Project untrusted event values onto the production audit schema."""

import re

from pydantic import JsonValue

from aftersales.policy.output_guard import OUTPUT_VIOLATION_CODES

_PHONE = re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)")
_FIELDS = {
    "action_id",
    "action",
    "actions",
    "allowed",
    "amount_cents",
    "arguments",
    "cache_creation_tokens",
    "cache_hit",
    "cached_tokens",
    "codes",
    "cost",
    "currency",
    "enabled",
    "execution_id",
    "input_tokens",
    "is_error",
    "latency_ms",
    "model",
    "name",
    "new_sku_id",
    "order_id",
    "order_item_id",
    "output_tokens",
    "permission",
    "permanent",
    "proposal",
    "proposal_id",
    "purpose",
    "qty",
    "reason",
    "requires_approval",
    "result",
    "role",
    "rule_ids",
    "rules",
    "run_id",
    "sku_id",
    "source",
    "status",
    "step",
    "steps",
    "stop_reason",
    "tokens",
    "tool_name",
    "truncated",
    "turn_id",
    "user_coupon_id",
    "version",
    "items",
    "address",
    "default_address",
    "phone",
}
_IDS = {
    "order_id": r"O\d+",
    "order_item_id": r"OI\d+",
    "sku_id": r"S\d+",
    "new_sku_id": r"S\d+",
    "user_coupon_id": r"UC\d+",
    "proposal_id": r"P\d+",
    "action_id": r"A\d+",
    "run_id": r"[a-f0-9]{32}",
    "turn_id": r"[a-f0-9]{32}",
    "execution_id": r"(?:[a-f0-9]{32}:\d+|[a-f0-9]{64})",
    "rule_ids": r"(?:P\d{2}|DATA|STOCK)",
    "rules": r"(?:P\d{2}|DATA|STOCK)",
}
_TOOLS = {
    "find_user_by_phone",
    "find_user_by_order",
    "get_user",
    "list_orders",
    "get_order",
    "get_product",
    "get_price_history",
    "list_user_coupons",
    "lookup_policy",
    "calculate",
    "preview_refund",
    "propose_actions",
    "modify_address",
    "cancel_order",
    "apply_price_protection",
    "request_return",
    "request_exchange",
    "create_order",
    "submit_approval",
    "transfer_to_human",
    "update_todo",
}
_VALUES = {
    "action": _TOOLS,
    "name": _TOOLS,
    "tool_name": _TOOLS,
    "permission": {"read", "write", "escalate"},
    "currency": {"USD"},
    "codes": OUTPUT_VIOLATION_CODES,
    "purpose": {"agent", "confirmation", "goal_judge"},
    "role": {"user", "assistant", "tool"},
    "source": {"hook", "service", "execution", "web_button", "natural_language"},
    "reason": {
        "completed",
        "processing",
        "success",
        "failed",
        "dead_letter",
        "disconnected",
        "error",
        "end_turn",
        "max_steps",
        "handoff",
        "session_ended",
    },
    "stop_reason": {
        "end_turn",
        "tool_use",
        "max_tokens",
        "max_steps",
        "handoff",
        "error",
        "session_ended",
    },
    "status": {
        "pending",
        "approved",
        "rejected",
        "invalidated",
        "expired",
        "consumed",
        "executed",
        "awaiting_payment",
        "updated",
        "cancelled",
        "pending_shipment",
        "completed",
        "allowed",
        "denied",
        "disabled",
        "passed",
        "retry",
        "fallback",
    },
}
_LABEL = re.compile(r"[A-Za-z0-9_./:-]{1,100}")
_COST = re.compile(r"\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?")


def redact_audit(data: dict[str, JsonValue], *, model_event: bool = False) -> dict[str, JsonValue]:
    """Retain bounded metadata; free text, arbitrary JSON and credentials are excluded."""

    def redact(key: str, value: JsonValue) -> JsonValue:
        if key in {"address", "default_address"}:
            return "[已脱敏]"
        if key == "phone":
            match = _PHONE.fullmatch(value) if isinstance(value, str) else None
            return f"{match[1]}****{match[2]}" if match else "[已脱敏]"
        if isinstance(value, dict):
            return redact_audit(value)
        if isinstance(value, list):
            return [redact(key, item) for item in value]
        if isinstance(value, str):
            if key == "cost" and model_event and _COST.fullmatch(value):
                return value
            if (
                key == "model"
                and model_event
                and _LABEL.fullmatch(value)
                and not value.startswith(("sk-", "eyJ", "Bearer"))
                and not _PHONE.search(value)
            ):
                return value
            if value in _VALUES.get(key, ()) or (
                key in _IDS and re.fullmatch(_IDS[key], value) and not _PHONE.search(value)
            ):
                return value
            return "[已脱敏]"
        if isinstance(value, int) and _PHONE.fullmatch(str(value)):
            return "[已脱敏]"
        return value

    return {key: redact(key, value) for key, value in data.items() if key in _FIELDS}

"""Production trace projection stays safe without infrastructure dependencies."""

from aftersales.core.privacy import redact_audit


def test_audit_excludes_free_text_credentials_and_address_details() -> None:
    redacted = redact_audit(
        {
            "content": "My token is eyJ.private.signature",
            "raw": {"authorization": "Bearer another-key", "api_key": "a third key"},
            "arguments": {
                "phone": "13800000042",
                "address": {"detail": "示例路42号"},
                "order_id": "O1006",
                "qty": 13800000042,
                "name": "opaque-key",
                "model": "opaque-model-shaped-key",
            },
            "rule_ids": ["P01"],
        }
    )
    assert redacted == {
        "arguments": {
            "phone": "138****0042",
            "address": "[已脱敏]",
            "order_id": "O1006",
            "qty": "[已脱敏]",
            "name": "[已脱敏]",
            "model": "[已脱敏]",
        },
        "rule_ids": ["P01"],
    }

"""Multiset business snapshots with order-component identities for generated keys."""

import hashlib
import json
import sqlite3
from collections import Counter

from pydantic import BaseModel, Field, JsonValue

BUSINESS_TABLES = (
    "orders",
    "order_items",
    "skus",
    "user_coupons",
    "aftersales",
    "refunds",
    "approvals",
    "handoffs",
)
Row = dict[str, JsonValue]
FREE_TEXT = {
    "aftersales": ("reason",),
    "approvals": ("reason",),
    "handoffs": ("summary", "reason"),
}


def encoded(value: JsonValue) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(value: JsonValue) -> str:
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def business_rows(db: sqlite3.Connection) -> dict[str, list[Row]]:
    return {
        table: [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
        for table in BUSINESS_TABLES
    }


class StateSnapshot(BaseModel):
    rows: dict[str, list[Row]]
    invalid_text: dict[str, list[Row]] = Field(default_factory=dict)


class TableDiff(BaseModel):
    table: str
    missing: list[Row] = Field(default_factory=list)
    extra: list[Row] = Field(default_factory=list)


def normalize_state(db: sqlite3.Connection, initial: dict[str, set[str]]) -> StateSnapshot:
    """Keep seeded resource anchors; identify new orders by their complete components.

    Child refunds are included before aftersale/line/order identities are assigned.
    This distinguishes identical headers with different baskets or refund links.
    Back references inside approval JSON are localized to avoid cycles.
    """
    rows = business_rows(db)
    indexed = {table: {row["id"]: row for row in records} for table, records in rows.items()}

    def clean(row: Row, *excluded: str) -> Row:
        # Approval args.reason follows the same free-text contract as top-level reason.
        result = {
            k: v
            for k, v in row.items()
            if k not in {"id", "created_at", "reason", "summary", *excluded}
        }
        for key in ("address", "args"):
            if key in result and isinstance(result[key], str):
                result[key] = json.loads(result[key])
        if "action" in row and isinstance(result.get("args"), dict):
            result["args"] = {k: v for k, v in result["args"].items() if k != "reason"}
        return result

    def anchor(table: str, record_id: str) -> str | None:
        return f"seed:{table}:{record_id}" if record_id in initial[table] else None

    def child_refunds(aftersale_id: str) -> list[Row]:
        return sorted(
            [
                clean(row, "order_id", "aftersale_id")
                for row in rows["refunds"]
                if row["aftersale_id"] == aftersale_id
            ],
            key=encoded,
        )

    def sale_fragment(row: Row) -> Row:
        return {**clean(row, "order_id", "order_item_id"), "refunds": child_refunds(row["id"])}

    def line_fragment(row: Row) -> Row:
        return {
            **clean(row, "order_id"),
            "anchor": anchor("order_items", row["id"]),
            "aftersales": sorted(
                [
                    sale_fragment(sale)
                    for sale in rows["aftersales"]
                    if sale["order_item_id"] == row["id"]
                ],
                key=encoded,
            ),
        }

    references = {
        "order_id": "orders",
        "used_order_id": "orders",
        "order_item_id": "order_items",
        "aftersale_id": "aftersales",
    }

    def local_args(value: JsonValue, key: str = "") -> JsonValue:
        if isinstance(value, dict):
            return {k: local_args(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [local_args(v, key) for v in value]
        table = references.get(key)
        if isinstance(value, str) and table and value in indexed[table]:
            row = indexed[table][value]
            return anchor(table, value) or {
                "local": table,
                "fields": clean(row, "order_id", "order_item_id"),
            }
        return value

    order_keys: dict[str, str] = {}
    for row in rows["orders"]:
        order_id = row["id"]
        component = {
            "fields": clean(row),
            "lines": sorted(
                [
                    line_fragment(item)
                    for item in rows["order_items"]
                    if item["order_id"] == order_id
                ],
                key=encoded,
            ),
            "refunds": sorted(
                [
                    clean(refund, "order_id", "aftersale_id")
                    for refund in rows["refunds"]
                    if refund["order_id"] == order_id and refund["aftersale_id"] is None
                ],
                key=encoded,
            ),
            "approvals": sorted(
                [
                    local_args(clean(approval, "order_id"))
                    for approval in rows["approvals"]
                    if approval["order_id"] == order_id
                ],
                key=encoded,
            ),
        }
        order_keys[order_id] = anchor("orders", order_id) or f"new:orders:{fingerprint(component)}"
    line_keys = {
        row["id"]: anchor("order_items", row["id"])
        or "new:order_items:"
        + fingerprint({"order": order_keys[row["order_id"]], "line": line_fragment(row)})
        for row in rows["order_items"]
    }
    sale_keys = {
        row["id"]: anchor("aftersales", row["id"])
        or "new:aftersales:"
        + fingerprint({"line": line_keys[row["order_item_id"]], "sale": sale_fragment(row)})
        for row in rows["aftersales"]
    }
    keys = {"orders": order_keys, "order_items": line_keys, "aftersales": sale_keys}

    def resolve(value: JsonValue, key: str = "") -> JsonValue:
        if isinstance(value, dict):
            return {k: resolve(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v, key) for v in value]
        table = references.get(key)
        if isinstance(value, str) and table:
            return keys[table].get(value, f"dangling:{table}:{value}")
        return value

    normalized: dict[str, list[Row]] = {}
    for table, records in rows.items():
        result = []
        for row in records:
            if table == "skus":
                item = {"resource": row["id"], "stock": row["stock"]}
            else:
                item = resolve(clean(row))
                stable = anchor(table, row["id"])
                if stable:
                    item["resource"] = stable
            result.append(item)
        normalized[table] = sorted(result, key=encoded)
    invalid = {
        table: [
            {"resource": row["id"], "invalid_field": field}
            for row in rows[table]
            for field in fields
            if not isinstance(row.get(field), str) or not row[field].strip()
        ]
        for table, fields in FREE_TEXT.items()
    }
    for row in rows["approvals"]:
        args = json.loads(row["args"])
        if "reason" in args and (not isinstance(args["reason"], str) or not args["reason"].strip()):
            invalid["approvals"].append({"resource": row["id"], "invalid_field": "args.reason"})
    return StateSnapshot(rows=normalized, invalid_text=invalid)


def seed_anchors(db: sqlite3.Connection) -> dict[str, set[str]]:
    return {
        table: {row[0] for row in db.execute(f"SELECT id FROM {table}")}
        for table in BUSINESS_TABLES
    }


def compare_states(actual: StateSnapshot, expected: StateSnapshot) -> list[TableDiff]:
    differences = []
    for table in BUSINESS_TABLES:
        left = Counter(map(encoded, actual.rows[table]))
        right = Counter(map(encoded, expected.rows[table]))
        invalid_actual = actual.invalid_text.get(table, [])
        invalid_expected = expected.invalid_text.get(table, [])
        if left != right or invalid_actual or invalid_expected:
            differences.append(
                TableDiff(
                    table=table,
                    missing=[json.loads(row) for row in (right - left).elements()]
                    + invalid_expected,
                    extra=[json.loads(row) for row in (left - right).elements()] + invalid_actual,
                )
            )
    return differences

"""SQLite setup, atomic service transactions, deterministic IDs and table dumps."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, TypeGuard

if TYPE_CHECKING:
    from aftersales.domain.storage import PgConnection

    DbConnection = sqlite3.Connection | PgConnection
else:
    DbConnection = sqlite3.Connection

TABLES = (
    "users",
    "payment_methods",
    "spus",
    "skus",
    "price_history",
    "coupons",
    "user_coupons",
    "orders",
    "order_items",
    "shipments",
    "aftersales",
    "refunds",
    "approvals",
    "handoffs",
)
ID_FORMATS = {
    "users": ("U", 3, 1),
    "payment_methods": ("PM", 4, 1),
    "spus": ("SPU", 2, 1),
    "skus": ("S", 4, 1),
    "coupons": ("C", 2, 1),
    "user_coupons": ("UC", 4, 1),
    "orders": ("O", 4, 1001),
    "order_items": ("OI", 5, 1),
    "aftersales": ("AS", 4, 1),
    "refunds": ("R", 4, 1),
    "approvals": ("AP", 4, 1),
    "handoffs": ("H", 4, 1),
}


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(str(path), isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def initialize(db: sqlite3.Connection) -> None:
    db.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))


def ensure_read_indexes(db: sqlite3.Connection) -> None:
    """Apply additive indexes to databases seeded with an earlier release."""
    ddl = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    db.executescript(ddl.split("-- Read indexes\n", 1)[1])


def is_postgres(db: DbConnection) -> TypeGuard[PgConnection]:
    return getattr(db, "dialect", None) == "postgresql"


def insert(db: DbConnection, table: str, **values: str | int | None) -> None:
    if table not in TABLES:
        raise ValueError("Unknown table")
    columns = (
        {name for name in values if name.isidentifier()}
        if is_postgres(db)
        else {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    )
    if not values or not values.keys() <= columns:
        raise ValueError("Unknown columns")
    names = ", ".join(values)
    placeholders = ", ".join("?" for name in values)
    db.execute(f"INSERT INTO {table} ({names}) VALUES ({placeholders})", tuple(values.values()))


@contextmanager
def transaction(db: DbConnection) -> Iterator[None]:
    """Support caller-owned transactions without committing their work."""
    if is_postgres(db):
        with db.transaction():
            yield
        return
    nested = db.in_transaction
    db.execute("SAVEPOINT domain_write" if nested else "BEGIN IMMEDIATE")
    try:
        yield
        db.execute("RELEASE domain_write" if nested else "COMMIT")
    except BaseException:
        if nested:
            db.execute("ROLLBACK TO domain_write")
            db.execute("RELEASE domain_write")
        elif db.in_transaction:
            db.execute("ROLLBACK")
        raise


def next_id(db: DbConnection, table: str) -> str:
    """Allocate inside the caller's transaction; identifiers are allowlisted."""
    prefix, width, start = ID_FORMATS[table]
    if is_postgres(db):
        number = db.execute(f"SELECT nextval('{table}_id_seq')").fetchone()[0]
        return f"{prefix}{number:0{width}d}"
    maximum = max(
        (int(row[0][len(prefix) :]) for row in db.execute(f"SELECT id FROM {table}")),
        default=start - 1,
    )
    return f"{prefix}{maximum + 1:0{width}d}"


def sequence_id_sql(table: str) -> str:
    """Return a per-row PostgreSQL ID expression with a minimum display width."""
    prefix, width, _ = ID_FORMATS[table]
    # A PostgreSQL sequence is a positive bigint with at most 19 digits.
    mask = "FM" + "9" * (19 - width) + "0" * width
    return f"'{prefix}' || to_char(nextval('{table}_id_seq'), '{mask}')"


def insert_generated_id(db: PgConnection, table: str, **values: str | int | None) -> str:
    if table not in ID_FORMATS or not values or "id" in values:
        raise ValueError("Invalid generated ID insert")
    if not all(name.isidentifier() for name in values):
        raise ValueError("Unknown columns")
    names = ", ".join(values)
    placeholders = ", ".join("?" for _ in values)
    row = db.execute(
        f"INSERT INTO {table} (id, {names}) VALUES ({sequence_id_sql(table)}, {placeholders})"
        " RETURNING id",
        tuple(values.values()),
    ).fetchone()
    return row[0]


def table_dump(db: sqlite3.Connection, table: str) -> bytes:
    if table not in TABLES:
        raise ValueError("Unknown table")
    rows = [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
    encoded = sorted(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in rows)
    return ("\n".join(encoded) + "\n").encode("utf-8")


def dump_hashes(db: sqlite3.Connection) -> dict[str, str]:
    return {table: hashlib.sha256(table_dump(db, table)).hexdigest() for table in TABLES}

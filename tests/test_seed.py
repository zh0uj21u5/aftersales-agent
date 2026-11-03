"""Deterministic table dumps, database constraints, fixture scenarios and CLI."""

import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aftersales.cli import app
from aftersales.domain.clock import FIXED_NOW, FixedClock
from aftersales.domain.db import TABLES, connect, dump_hashes, initialize, next_id, transaction
from aftersales.domain.seed import seed_data, seed_database


@pytest.fixture
def shop() -> Iterator[sqlite3.Connection]:
    db = connect()
    initialize(db)
    seed_data(db)
    yield db
    db.close()


def test_clock_is_fixed_and_rejects_naive_time() -> None:
    assert FixedClock().now().isoformat() == "2026-11-15T12:00:00+08:00"
    assert FixedClock().now() == FixedClock().now() == FIXED_NOW
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock(datetime(2026, 11, 15))


def test_seed_table_dump_hashes_are_identical_and_different_seed_changes_data(
    tmp_path: Path,
) -> None:
    snapshots = []
    database_bytes = []
    for index, seed in enumerate((42, 42, 43)):
        path = tmp_path / f"shop{index}.db"
        seed_database(path, seed)
        database_bytes.append(path.read_bytes())
        with closing(connect(path)) as db:
            snapshots.append(dump_hashes(db))
    assert snapshots[0] == snapshots[1]
    assert database_bytes[0] == database_bytes[1]
    assert snapshots[0] != snapshots[2]
    assert set(snapshots[0]) == set(TABLES) and len(snapshots[0]) == 14


def test_counts_statuses_and_foreign_keys(shop: sqlite3.Connection) -> None:
    for table, count in [("users", 50), ("spus", 30), ("skus", 120), ("orders", 300)]:
        assert shop.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == count
    assert shop.execute("SELECT count(DISTINCT status) FROM orders").fetchone()[0] == 7
    assert shop.execute("PRAGMA foreign_key_check").fetchall() == []
    assert shop.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert {
        row[0] for row in shop.execute("SELECT name FROM sqlite_master WHERE type='table'")
    } == set(TABLES)


def test_required_policy_scenarios_exist(shop: sqlite3.Connection) -> None:
    assert (
        shop.execute(
            "SELECT count(*) FROM price_history WHERE effective_at=?",
            ("2026-11-14T12:00:00+08:00",),
        ).fetchone()[0]
        == 120
    )
    assert (
        shop.execute("SELECT paid_at FROM orders WHERE id='O1001'").fetchone()[0]
        == "2026-11-07T12:00:00+08:00"
    )
    assert (
        shop.execute("SELECT paid_at FROM orders WHERE id='O1002'").fetchone()[0]
        == "2026-11-08T12:00:00+08:00"
    )
    assert (
        shop.execute("SELECT count(*) FROM order_items WHERE order_id='O1006'").fetchone()[0] == 2
    )
    for order_id, category in [("O1003", "fresh"), ("O1004", "custom")]:
        row = shop.execute(
            """SELECT p.category, o.status FROM orders o
            JOIN order_items i ON i.order_id=o.id JOIN skus s ON s.id=i.sku_id
            JOIN spus p ON p.id=s.spu_id WHERE o.id=?""",
            (order_id,),
        ).fetchone()
        assert tuple(row) == (category, "delivered")
    assert shop.execute("SELECT paid_cents FROM orders WHERE id='O1005'").fetchone()[0] > 20000
    assert shop.execute("SELECT stock FROM skus WHERE id='S0004'").fetchone()[0] == 0
    for order_id, coupon_id in [("O1007", "C02"), ("O1008", "C01")]:
        row = shop.execute(
            """SELECT uc.coupon_id FROM orders o JOIN user_coupons uc
            ON uc.id=o.user_coupon_id WHERE o.id=?""",
            (order_id,),
        ).fetchone()
        assert row[0] == coupon_id
    injection_count = shop.execute(
        "SELECT count(*) FROM spus WHERE description LIKE '%系统通知%'"
    ).fetchone()[0]
    injection_count += shop.execute(
        "SELECT count(*) FROM orders WHERE note LIKE '%系统通知%'"
    ).fetchone()[0]
    assert injection_count >= 5
    assert (
        shop.execute(
            "SELECT count(*) FROM orders WHERE status='shipped' AND paid_at<?",
            ("2026-11-08T12:00:00+08:00",),
        ).fetchone()[0]
        > 0
    )


def test_seed_money_is_reconciled_for_all_orders(shop: sqlite3.Connection) -> None:
    # Each line = unit price * qty; sum(line) - discount = sum(paid shares).
    # Unpaid orders store 0 paid cents; every paid order equals that sum exactly.
    for row in shop.execute("""SELECT o.*, sum(i.unit_price_cents*i.qty) AS total,
        sum(i.paid_share_cents) AS shares FROM orders o
        JOIN order_items i ON i.order_id=o.id GROUP BY o.id"""):
        assert row["items_total_cents"] == row["total"]
        assert row["total"] - row["discount_cents"] == row["shares"]
        assert row["paid_cents"] == (0 if row["paid_at"] is None else row["shares"])


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE skus SET stock=-1 WHERE id='S0001'",
        "UPDATE skus SET price_cents=1.5 WHERE id='S0001'",
        "UPDATE orders SET status='unknown' WHERE id='O1001'",
        "UPDATE orders SET price_protected=2 WHERE id='O1001'",
        "UPDATE orders SET address='invalid json' WHERE id='O1001'",
        "UPDATE order_items SET returned_qty=qty+1 WHERE id='OI00001'",
        "UPDATE order_items SET refunded_cents=paid_share_cents+1 WHERE id='OI00001'",
        "UPDATE refunds SET payment_method_id='PM0001' WHERE payment_method_id!='PM0001'",
    ],
)
def test_schema_rejects_invalid_data(shop: sqlite3.Connection, sql: str) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        shop.execute(sql)


def test_ids_and_rollback(shop: sqlite3.Connection) -> None:
    assert next_id(shop, "orders") == "O1301"
    assert re.fullmatch(r"OI\d{5}", next_id(shop, "order_items"))
    previous = dump_hashes(shop)
    with pytest.raises(RuntimeError), transaction(shop):
        shop.execute("UPDATE skus SET stock=0")
        raise RuntimeError("rollback")
    assert dump_hashes(shop) == previous
    with transaction(shop), pytest.raises(RuntimeError), transaction(shop):
        shop.execute("UPDATE skus SET stock=0")
        raise RuntimeError("nested rollback")
    assert dump_hashes(shop) == previous


def test_seed_cli_creates_database_and_preserves_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "cli.db"
    result = CliRunner().invoke(app, ["seed", "--db", str(path), "--seed", "42"])
    assert result.exit_code == 0, result.output
    original = path.read_bytes()
    result = CliRunner().invoke(app, ["seed", "--db", str(path)])
    assert result.exit_code != 0 and path.read_bytes() == original


def test_policy_sections_preserve_rule_ids_and_cover_all_topics() -> None:
    policy = (Path(__file__).parents[1] / "src/aftersales/policy/POLICY.md").read_text(
        encoding="utf-8"
    )
    sections = re.split(r"(?m)^## ", policy)[1:]
    assert [re.match(r"P(\d+)", section).group(1) for section in sections] == [
        f"{i:02}" for i in range(1, 16)
    ]
    assert all(re.search(r"<!-- topic: \w+ -->", section) for section in sections)
    assert set(re.findall(r"<!-- topic: (\w+) -->", policy)) == {
        "auth",
        "confirm",
        "address_cancel",
        "price_protection",
        "return",
        "refund",
        "exchange",
        "coupon",
        "approval",
        "security",
        "escalation",
    }

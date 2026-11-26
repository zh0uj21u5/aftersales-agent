"""Apply production schema migrations and optionally load demonstration data."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from aftersales.domain.clock import Clock, WallClock
from aftersales.domain.db import ID_FORMATS, transaction
from aftersales.domain.seed import seed_data
from aftersales.domain.storage import PgConnection, PostgresStorage

MIGRATIONS = ("pg_schema.sql", "pg_migration_2.sql")


def migrate(
    db: PgConnection,
    *,
    seed: bool = False,
    clock: Clock | None = None,
    target: int = len(MIGRATIONS),
) -> None:
    if not 1 <= target <= len(MIGRATIONS):
        raise ValueError("Unknown migration target")
    with transaction(db):
        db.execute("SELECT pg_advisory_xact_lock(7357296900)")
        db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        applied = {row[0] for row in db.execute("SELECT version FROM schema_migrations")}
        for version, filename in enumerate(MIGRATIONS[:target], 1):
            if version not in applied:
                db.execute(Path(__file__).with_name(filename).read_text(encoding="utf-8"))
                db.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
        if seed and db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
            seed_data(db, clock=clock or WallClock())
            for table, (prefix, _, start) in ID_FORMATS.items():
                maximum = db.execute(
                    f"SELECT max(substring(id, ?)::bigint) FROM {table}", (len(prefix) + 1,)
                ).fetchone()[0]
                db.execute(
                    "SELECT setval(?::regclass, ?, ?)",
                    (
                        f"{table}_id_seq",
                        maximum if maximum is not None else start,
                        maximum is not None,
                    ),
                )


async def _main(seed: bool) -> None:
    storage = PostgresStorage(os.environ["DATABASE_URL"])
    async with storage.pool:
        await storage.run(lambda db: migrate(db, seed=seed))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", action="store_true")
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(_main(parser.parse_args().seed))

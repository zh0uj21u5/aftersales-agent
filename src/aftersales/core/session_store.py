"""SQLite session checkpoints, separate from the fourteen benchmark tables."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from aftersales.domain.clock import Clock
from aftersales.domain.db import DbConnection, transaction

if TYPE_CHECKING:
    from aftersales.core.loop import Session


class SessionConflict(ValueError):
    """A stale checkpoint must never overwrite a newer conversation."""


T = TypeVar("T")


class StoreRunner(Protocol):
    async def __call__(self, work: Callable[[SessionStore], T]) -> T: ...


class SessionStore:
    db: DbConnection

    def __init__(self, db: sqlite3.Connection, clock: Clock, *, initialize: bool = True) -> None:
        self.db, self.clock = db, clock
        if initialize:
            self.initialize()

    def initialize(self) -> None:
        db = self.db
        # Do not use executescript: it implicitly commits caller-owned transactions.
        db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY, state_json TEXT NOT NULL,
            revision INTEGER NOT NULL, updated_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS action_executions (
            execution_key TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES sessions(session_id),
            proposal_id TEXT NOT NULL, action_id TEXT NOT NULL, version INTEGER NOT NULL,
            tool_name TEXT NOT NULL, canonical_args_json TEXT NOT NULL, args_hash TEXT NOT NULL,
            result_json TEXT NOT NULL, before_snapshot_json TEXT NOT NULL,
            after_snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(session_id, proposal_id, action_id))""")
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_action_executions_session ON action_executions(session_id)"
        )
        db.execute("""CREATE TABLE IF NOT EXISTS session_traces (
            session_id TEXT NOT NULL REFERENCES sessions(session_id), run_id TEXT NOT NULL,
            run_number INTEGER NOT NULL,
            PRIMARY KEY(session_id, run_id))""")
        if not self._numbered_traces():
            with transaction(db):
                if not self._numbered_traces():
                    db.execute(
                        "ALTER TABLE session_traces ADD COLUMN run_number INTEGER NOT NULL DEFAULT 0"
                    )
                    db.execute("UPDATE session_traces SET run_number=rowid")
        db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_session_traces_number
            ON session_traces(session_id, run_number)""")

    def _numbered_traces(self) -> bool:
        return any(
            row[1] == "run_number" for row in self.db.execute("PRAGMA table_info(session_traces)")
        )

    def trace_runs(
        self, session_id: str, *, first_run: int = 1, newest_first: bool = False, limit: int = -1
    ) -> list[tuple[int, str]]:
        order = "DESC" if newest_first else "ASC"
        bound, params = ("LIMIT ?", (limit,)) if limit >= 0 else ("", ())
        return [
            (row[0], row[1])
            for row in self.db.execute(
                f"""SELECT run_number, run_id FROM session_traces
                WHERE session_id=? AND run_number>=? ORDER BY run_number {order} {bound}""",
                (session_id, first_run, *params),
            )
        ]

    def __deepcopy__(self, memo: dict[int, Any]) -> SessionStore:
        # Shadow auditing copies Session; its connection remains an injected resource.
        return self

    def save(self, session: Session) -> None:
        stamp = self.clock.now().isoformat()
        state = session.model_dump(mode="json")
        state["updated_at"] = stamp
        import json

        encoded = json.dumps(state, ensure_ascii=False, allow_nan=False)
        if session._revision is None:
            try:
                self.db.execute(
                    "INSERT INTO sessions VALUES (?, ?, 1, ?)",
                    (session.session_id, encoded, stamp),
                )
            except sqlite3.IntegrityError as exc:
                raise SessionConflict("会话已存在，请重新读取最新状态") from exc
            revision = 1
        else:
            cursor = self.db.execute(
                """UPDATE sessions SET state_json=?, revision=revision+1, updated_at=?
                WHERE session_id=? AND revision=?""",
                (encoded, stamp, session.session_id, session._revision),
            )
            if cursor.rowcount != 1:
                raise SessionConflict("会话已被更新，请重新读取最新状态")
            revision = session._revision + 1
        session.updated_at = stamp
        session._revision, session._store = revision, self

    def require_session(self, session_id: str) -> None:
        if (
            self.db.execute("SELECT 1 FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            is None
        ):
            raise KeyError(session_id)

    def lock_checkpoint(self, session: Session) -> None:
        """SQLite's outer write transaction already serializes checkpoint writes."""

    def load(self, session_id: str, *, attach: bool = True) -> Session:
        from aftersales.core.loop import Session

        row = self.db.execute(
            "SELECT state_json, revision FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        session = Session.model_validate_json(row[0])
        session._revision, session._store = row[1], self if attach else None
        return session

    def attach_trace(self, session_id: str, run_id: str) -> int:
        row = self.db.execute(
            """INSERT INTO session_traces (session_id, run_id, run_number)
            VALUES (?, ?, (SELECT coalesce(max(run_number), 0) + 1
                FROM session_traces WHERE session_id=?))
            RETURNING run_number""",
            (session_id, run_id, session_id),
        ).fetchone()
        return row[0]

    def trace_ids(self, session_id: str) -> list[str]:
        return [
            row[0]
            for row in self.db.execute(
                "SELECT run_id FROM session_traces WHERE session_id=? ORDER BY run_number",
                (session_id,),
            )
        ]

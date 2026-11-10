"""SQLite session checkpoints, separate from the fourteen benchmark tables."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Any

from aftersales.domain.clock import Clock

if TYPE_CHECKING:
    from aftersales.core.loop import Session


class SessionConflict(ValueError):
    """A stale checkpoint must never overwrite a newer conversation."""


class SessionStore:
    def __init__(self, db: sqlite3.Connection, clock: Clock) -> None:
        self.db, self.clock = db, clock
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
        db.execute("""CREATE TABLE IF NOT EXISTS session_traces (
            session_id TEXT NOT NULL REFERENCES sessions(session_id), run_id TEXT NOT NULL,
            PRIMARY KEY(session_id, run_id))""")

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

    def load(self, session_id: str) -> Session:
        from aftersales.core.loop import Session

        row = self.db.execute(
            "SELECT state_json, revision FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        session = Session.model_validate_json(row[0])
        session._revision, session._store = row[1], self
        return session

    def attach_trace(self, session_id: str, run_id: str) -> None:
        self.db.execute("INSERT INTO session_traces VALUES (?, ?)", (session_id, run_id))

    def trace_ids(self, session_id: str) -> list[str]:
        return [
            row[0]
            for row in self.db.execute(
                "SELECT run_id FROM session_traces WHERE session_id=? ORDER BY rowid", (session_id,)
            )
        ]

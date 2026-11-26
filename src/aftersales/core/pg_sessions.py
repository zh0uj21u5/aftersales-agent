"""PostgreSQL session heads and append-only conversation checkpoints."""

import json

from aftersales.core.llm import Message
from aftersales.core.loop import Session
from aftersales.core.session_store import SessionConflict, SessionStore
from aftersales.domain.clock import Clock
from aftersales.domain.db import transaction
from aftersales.domain.storage import PgConnection


def _encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class PgSessionStore(SessionStore):
    db: PgConnection

    def __init__(self, db: PgConnection, clock: Clock) -> None:
        self.db, self.clock = db, clock

    def require_owner(self, session_id: str, owner: str) -> int:
        row = self.db.execute(
            "SELECT revision FROM sessions WHERE session_id=? AND owner_user_id=?",
            (session_id, owner),
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return row[0]

    def lock_checkpoint(self, session: Session) -> None:
        row = self.db.execute(
            "SELECT revision FROM sessions WHERE session_id=? AND owner_user_id=? FOR NO KEY UPDATE",
            (session.session_id, session.owner_user_id),
        ).fetchone()
        if row is None or row[0] != session._revision:
            raise SessionConflict("会话已被更新，请重新读取最新状态")

    def save(self, session: Session) -> None:
        owner = session.owner_user_id
        if not owner or owner != session.authenticated_user_id:
            raise SessionConflict("登录身份与会话归属不匹配")
        saved = session._persisted_messages
        count = len(session.messages)
        if count < saved:
            raise SessionConflict("不能覆盖历史会话消息")
        stamp = self.clock.now().isoformat()
        head = session.model_dump(mode="json", exclude={"messages", "proposals"})
        head["updated_at"] = stamp
        proposals = _encode([p.model_dump(mode="json") for p in session.proposals])
        with transaction(self.db):
            if session._revision is None:
                cursor = self.db.execute(
                    """INSERT INTO sessions
                      (session_id, owner_user_id, revision, updated_at, state_json,
                       proposals_json, message_count)
                    VALUES (?, ?, 1, ?, ?, ?, ?) ON CONFLICT (session_id) DO NOTHING""",
                    (session.session_id, owner, stamp, _encode(head), proposals, count),
                )
                revision = 1
            else:
                cursor = self.db.execute(
                    """UPDATE sessions SET state_json=?, proposals_json=?, message_count=?,
                       updated_at=?, revision=revision+1 WHERE session_id=? AND owner_user_id=?
                       AND revision=? AND message_count=?""",
                    (
                        _encode(head),
                        proposals,
                        count,
                        stamp,
                        session.session_id,
                        owner,
                        session._revision,
                        saved,
                    ),
                )
                revision = session._revision + 1
            if cursor.rowcount != 1:
                raise SessionConflict("会话已被更新，请重新读取最新状态")
            if count > saved:
                self.db.executemany(
                    "INSERT INTO session_messages VALUES (?, ?, ?)",
                    [
                        (session.session_id, index, message.model_dump_json())
                        for index, message in enumerate(session.messages[saved:], saved + 1)
                    ],
                )
        session.updated_at = stamp
        session._revision, session._store = revision, self
        session._persisted_messages = count

    def load(self, session_id: str, *, attach: bool = True) -> Session:
        head = self.db.execute(
            """SELECT owner_user_id, revision, state_json, proposals_json, message_count
                FROM sessions WHERE session_id=?""",
            (session_id,),
        ).fetchone()
        if head is None:
            raise KeyError(session_id)
        session = Session.model_validate(
            {**json.loads(head["state_json"]), "proposals": json.loads(head["proposals_json"])}
        )
        session.messages = [
            Message.model_validate_json(row[0])
            for row in self.db.execute(
                """SELECT message_json FROM session_messages WHERE session_id=? AND seq<=?
                   ORDER BY seq""",
                (session_id, head["message_count"]),
            )
        ]
        session._owner_user_id = head["owner_user_id"]
        session._revision = head["revision"]
        session._persisted_messages = head["message_count"]
        session._store = self if attach else None
        return session

    def attach_trace(self, session_id: str, run_id: str) -> int:
        row = self.db.execute(
            """WITH claimed AS (
                UPDATE sessions SET trace_run=trace_run+1 WHERE session_id=?
                RETURNING session_id, trace_run
            ) INSERT INTO session_traces (session_id, run_id, run_number)
              SELECT session_id, ?, trace_run FROM claimed RETURNING run_number""",
            (session_id, run_id),
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return row[0]

"""Session JSON round trips, optimistic writes and CLI resume integration."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from aftersales import cli
from aftersales.core.config import AgentConfig, LLMSettings
from aftersales.core.context import TodoItem
from aftersales.core.llm import LLMResponse, Message, MockLLM, ToolCall
from aftersales.core.loop import Session
from aftersales.core.runtime import build_agent
from aftersales.core.session_store import SessionConflict, SessionStore
from aftersales.core.tracing import TraceWriter
from aftersales.domain.clock import FixedClock
from aftersales.domain.db import connect, dump_hashes
from aftersales.domain.seed import seed_database
from aftersales.domain.services import Services
from aftersales.policy.proposal import ActionRequest, create_proposal, decide_proposal
from aftersales.tools.registry import ToolContext


def rich_session(shop: Services) -> Session:
    session = Session(
        authenticated_user_id="U006",
        todo=[TodoItem(id="1", content="修改地址", status="in_progress")],
        confirmation_user_index=2,
        confirmation_available=True,
        confirmation_checked=True,
        confirmation_identity="U006",
        confirmation_proposal="完整方案",
        confirmed_batch=True,
    )
    session.messages = [
        Message(role="user", content="查询订单"),
        Message(
            role="assistant",
            tool_calls=[ToolCall(id="c1", name="get_order", arguments={"order_id": "O1006"})],
        ),
        Message(role="tool", tool_call_id="c1", content="结果"),
        Message(role="assistant", content="请确认"),
        Message(role="user", content="同意"),
    ]
    ctx = ToolContext(session, AgentConfig(proposal_gate=True), shop.db, shop.clock)
    request = ActionRequest(
        tool_name="modify_address",
        arguments={
            "order_id": "O1006",
            "address": {**shop.get_order("O1006").address.model_dump(), "detail": "公司88号"},
        },
    )
    create_proposal(ctx, [request])
    create_proposal(ctx, [request])
    session.presented_proposal_version = 2
    return session


def test_complete_session_round_trip_across_instances(shop: Services) -> None:
    session = rich_session(shop)
    store = SessionStore(shop.db, shop.clock)
    store.save(session)
    restored = SessionStore(shop.db, shop.clock).load(session.session_id)
    assert restored.model_dump() == session.model_dump()
    assert restored.model_dump_json() == session.model_dump_json()
    assert restored.updated_at == shop.clock.now().isoformat()
    assert len(restored.proposals) == 2 and restored.proposals[0].status == "superseded"
    assert restored.messages[1].tool_calls[0].arguments == {"order_id": "O1006"}
    decide_proposal(restored, "P0002", 2, "approve")
    assert store.load(session.session_id).proposals[-1].status == "approved"
    restored.closed = True
    restored.persist()
    assert store.load(session.session_id).closed


def test_store_survives_new_database_connection(tmp_path: Path) -> None:
    path = tmp_path / "shop.db"
    seed_database(path)
    db = connect(path)
    session = rich_session(Services(db, FixedClock()))
    SessionStore(db, FixedClock()).save(session)
    expected = session.model_dump()
    db.close()
    db = connect(path)
    assert SessionStore(db, FixedClock()).load(session.session_id).model_dump() == expected
    db.close()


def test_stale_session_cannot_overwrite_newer_checkpoint(shop: Services) -> None:
    store = SessionStore(shop.db, shop.clock)
    session = rich_session(shop)
    store.save(session)
    stale = store.load(session.session_id)
    session.todo[0].status = "completed"
    store.save(session)
    with pytest.raises(SessionConflict):
        store.save(stale)
    assert store.load(session.session_id).todo[0].status == "completed"
    with pytest.raises(KeyError):
        store.load("missing")


def test_cli_resumes_messages_identity_todo_and_pending_proposal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "shop.db"
    seed_database(path)
    db = connect(path)
    session = rich_session(Services(db, FixedClock()))
    SessionStore(db, FixedClock()).save(session)
    db.close()
    mock = MockLLM([LLMResponse(text="继续处理现有方案")])
    monkeypatch.setattr(
        cli, "load_settings", lambda: LLMSettings(provider="deepseek", model="mock", api_key="test")
    )
    monkeypatch.setattr(cli, "make_client", lambda *args: mock)
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "chat",
            "--db",
            str(path),
            "--session",
            session.session_id,
            "--trace-dir",
            str(tmp_path / "traces"),
        ],
        input="继续\n/exit\n",
    )
    assert result.exit_code == 0, result.output
    assert session.session_id in result.output and "继续处理现有方案" in result.output
    db = connect(path)
    store = SessionStore(db, FixedClock())
    resumed = store.load(session.session_id)
    assert resumed.authenticated_user_id == "U006" and resumed.todo == session.todo
    assert resumed.messages[: len(session.messages)] == session.messages
    assert resumed.proposals == session.proposals
    assert len(store.trace_ids(session.session_id)) == 1
    db.close()
    missing = runner.invoke(cli.app, ["chat", "--db", str(path), "--session", "missing"])
    assert missing.exit_code != 0 and "会话不存在" in missing.output


def test_resume_closes_interrupted_batch_without_replaying_writes(
    shop: Services, tmp_path: Path
) -> None:
    session = Session(
        authenticated_user_id="U006",
        messages=[
            Message(role="user", content="查询订单"),
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(id="read", name="get_order", arguments={"order_id": "O1006"}),
                    ToolCall(
                        id="write",
                        name="cancel_order",
                        arguments={"order_id": "O1006", "reason": "取消"},
                    ),
                ],
            ),
            Message(role="tool", tool_call_id="read", content="已保存的查询结果"),
        ],
    )
    store = SessionStore(shop.db, shop.clock)
    store.save(session)
    mock = MockLLM([LLMResponse(text="已恢复，请继续说明需求")])
    trace = TraceWriter(shop.clock, tmp_path)
    agent = build_agent(shop.db, shop.clock, mock, AgentConfig(proposal_gate=True), trace)
    before = dump_hashes(shop.db)
    loaded = store.load(session.session_id)
    agent.respond(loaded, "继续")
    trace.close()
    messages = mock.requests[0].messages
    assert [m.role for m in messages] == ["user", "assistant", "tool", "tool", "user"]
    assert messages[-2].tool_call_id == "write" and messages[-2].is_error
    assert "INTERRUPTED" in messages[-2].content
    assert dump_hashes(shop.db) == before

"""HTTP integration with MockLLM, real SQLite and the unified authorization path."""

import json
import socket
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from aftersales.core.llm import LLMResponse, MockLLM, ToolCall
from aftersales.core.session_store import SessionStore
from aftersales.domain.clock import FixedClock
from aftersales.domain.db import connect, dump_hashes
from aftersales.domain.seed import seed_database
from aftersales.web.app import create_app


def using(tool_name: str, **args: Any) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use", tool_calls=[ToolCall(id=tool_name, name=tool_name, arguments=args)]
    )


@pytest.fixture
def api(tmp_path: Path) -> tuple[TestClient, MockLLM, Path]:
    database = tmp_path / "shop.db"
    seed_database(database)
    mock = MockLLM([])
    app = create_app(database, mock, trace_dir=tmp_path / "traces", report_dir=tmp_path / "reports")
    return TestClient(app), mock, database


def prepared(
    api: tuple[TestClient, MockLLM, Path],
    *,
    user: str | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]]:
    client, mock, _ = api
    created = client.post("/api/sessions", json={"authenticated_user_id": user})
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    if actions is None:
        actions = [
            {
                "tool_name": "apply_price_protection",
                "arguments": {"order_id": "O1006", "order_item_id": "OI00006"},
            }
        ]
    script = []
    if user is None:
        script.append(using("find_user_by_phone", phone="13800000006", name="用户006"))
    script += [
        using("propose_actions", actions=actions),
        LLMResponse(text="退差57.00元，请确认整份方案"),
    ]
    mock.script.extend(script)
    result = client.post(
        f"/api/sessions/{session_id}/messages", json={"content": "请认证并办理价保"}
    )
    assert result.status_code == 200, result.text
    state = result.json()["session"]
    assert state["proposals"], result.text
    return session_id, state


def test_authentication_proposal_button_and_refresh(api: tuple[TestClient, MockLLM, Path]) -> None:
    # O1006: first share=26000-1300=24700; (13000-10000)*2*24700/26000=5700.
    client, mock, database = api
    # Use credentials from the actual seeded user, independently from the model.
    db = connect(database)
    name = db.execute("SELECT name FROM users WHERE id='U006'").fetchone()[0]
    db.close()
    created = client.post("/api/sessions")
    session_id = created.json()["session_id"]
    assert created.json()["orders"] == []
    mock.script.extend(
        [
            using("find_user_by_phone", phone="13800000006", name=name),
            using(
                "propose_actions",
                actions=[
                    {
                        "tool_name": "apply_price_protection",
                        "arguments": {"order_id": "O1006", "order_item_id": "OI00006"},
                    }
                ],
            ),
            LLMResponse(text="为O1006退差57.00元，确认整份方案吗？"),
        ]
    )
    response = client.post(f"/api/sessions/{session_id}/messages", json={"content": "请办理价保"})
    assert response.status_code == 200, response.text
    state = client.get(f"/api/sessions/{session_id}").json()
    assert state["authenticated_user_id"] == "U006"
    assert all(order["orders"][0]["user_id"] == "U006" for order in state["orders"])
    proposal = state["proposals"][-1]
    assert (
        proposal["status"] == "pending" and proposal["actions"][0]["preview_amount_cents"] == 5700
    )
    decision_url = f"/api/sessions/{session_id}/proposals/{proposal['proposal_id']}/decision"
    approved = client.post(decision_url, json={"version": 1, "decision": "approve"})
    assert approved.status_code == 200, approved.text
    state = approved.json()
    assert state["proposals"][-1]["status"] == "consumed"
    record = state["action_executions"][0]
    assert record["before_snapshot"]["orders"][0]["price_protected"] == 0
    assert record["after_snapshot"]["orders"][0]["price_protected"] == 1
    assert record["result"]["amount_cents"] == 5700
    assert record["after_snapshot"]["refunds"][-1]["amount_cents"] == 5700
    assert record["after_snapshot"]["refunds"][-1]["payment_method_id"] == "PM0006"
    assert {e["event"] for e in state["trace_events"]} >= {
        "llm_call",
        "confirm_verdict",
        "tool_result",
    }
    assert client.get(f"/api/sessions/{session_id}").json() == state
    fresh = TestClient(create_app(database, MockLLM([]), trace_dir=database.parent / "traces"))
    assert fresh.get(f"/api/sessions/{session_id}").json() == state
    db = connect(database)
    before = dump_hashes(db)
    retried = fresh.post(decision_url, json={"version": 1, "decision": "approve"})
    assert retried.status_code == 200, retried.text
    assert retried.json()["action_executions"] == state["action_executions"]
    assert dump_hashes(db) == before
    db.close()


def test_reject_and_wrong_version_never_write(api: tuple[TestClient, MockLLM, Path]) -> None:
    client, _, database = api
    session_id, state = prepared(api, user="U006")
    url = f"/api/sessions/{session_id}/proposals/{state['proposals'][-1]['proposal_id']}/decision"
    db = connect(database)
    before = dump_hashes(db)
    bad = client.post(url, json={"version": 2, "decision": "approve"})
    assert bad.status_code == 409
    assert client.post(url, json={"version": True, "decision": "approve"}).status_code == 422
    rejected = client.post(url, json={"version": 1, "decision": "reject"})
    assert rejected.status_code == 200
    assert rejected.json()["proposals"][-1]["status"] == "rejected"
    assert rejected.json()["action_executions"] == []
    assert client.post(url, json={"version": 1, "decision": "approve"}).status_code == 409
    assert dump_hashes(db) == before
    db.close()


def test_deferred_approval_then_execute(api: tuple[TestClient, MockLLM, Path]) -> None:
    client, _, _ = api
    session_id, state = prepared(api, user="U006")
    url = f"/api/sessions/{session_id}/proposals/{state['proposals'][-1]['proposal_id']}/decision"
    approved = client.post(url, json={"version": 1, "decision": "approve", "execute": False})
    assert approved.json()["proposals"][-1]["status"] == "approved"
    assert approved.json()["action_executions"] == []
    executed = client.post(url, json={"version": 1, "decision": "approve", "execute": True})
    assert executed.status_code == 200 and len(executed.json()["action_executions"]) == 1


def test_button_honors_business_preconditions(api: tuple[TestClient, MockLLM, Path]) -> None:
    client, _, database = api
    session_id, _ = prepared(api, user="U006")
    db = connect(database)
    db.execute("UPDATE orders SET price_protected=1 WHERE id='O1006'")
    before = dump_hashes(db)
    response = client.post(
        f"/api/sessions/{session_id}/proposals/P0001/decision",
        json={"version": 1, "decision": "approve"},
    )
    assert response.status_code == 409 and "P04" in response.text
    assert dump_hashes(db) == before
    assert client.get(f"/api/sessions/{session_id}").json()["action_executions"] == []
    db.close()


def test_p08_card_does_not_refund(api: tuple[TestClient, MockLLM, Path]) -> None:
    # O1005 full return=45000 >20000; card approval alone must not issue a refund.
    client, _, database = api
    session_id, _ = prepared(
        api,
        user="U005",
        actions=[
            {
                "tool_name": "request_return",
                "arguments": {
                    "order_id": "O1005",
                    "items": [{"order_item_id": "OI00005", "qty": 3}],
                    "reason": "全退",
                },
            }
        ],
    )
    db = connect(database)
    before = dump_hashes(db)
    result = client.post(
        f"/api/sessions/{session_id}/proposals/P0001/decision",
        json={"version": 1, "decision": "approve"},
    )
    assert result.status_code == 200, result.text
    assert result.json()["action_executions"] == []
    assert result.json()["execution_notices"][0]["rule_id"] == "P08"
    assert result.json()["proposals"][-1]["actions"][0]["status"] == "approved"
    assert dump_hashes(db) == before
    db.close()


def test_reports_are_read_only_and_paths_confined(api: tuple[TestClient, MockLLM, Path]) -> None:
    client, mock, database = api
    directory = database.parent / "reports"
    directory.mkdir()
    path = directory / "run-1.json"
    data = {"run_id": "run-1", "metrics": {"pass^1": 1}, "results": []}
    path.write_text(json.dumps(data), encoding="utf-8")
    original = path.read_bytes()
    assert client.get("/api/evals/reports").json() == [{"run_id": "run-1"}]
    assert client.get("/api/evals/reports/run-1").json() == data
    assert client.get("/api/evals/reports/missing").status_code == 404
    assert client.get("/api/evals/reports/%2e%2e%5cshop").status_code == 404
    assert client.post("/api/evals/reports/run-1", json={}).status_code == 405
    assert path.read_bytes() == original and mock.requests == []


def test_missing_invalid_inputs_and_no_model_on_read(api: tuple[TestClient, MockLLM, Path]) -> None:
    client, mock, _ = api
    assert client.get("/api/evals/reports").json() == []
    assert client.get("/api/sessions/missing").status_code == 404
    assert (
        client.post("/api/sessions", json={"authenticated_user_id": "missing"}).status_code == 400
    )
    assert client.post("/api/sessions", json={"unexpected": True}).status_code == 422
    assert (
        client.post("/api/sessions/missing/messages", json={"content": "消息"}).status_code == 404
    )
    session_id = client.post("/api/sessions", json={}).json()["session_id"]
    assert (
        client.post(f"/api/sessions/{session_id}/messages", json={"content": " "}).status_code
        == 400
    )
    assert mock.requests == []


def test_closed_session_cannot_approve_new_actions(api: tuple[TestClient, MockLLM, Path]) -> None:
    client, _, database = api
    session_id, _ = prepared(api, user="U006")
    db = connect(database)
    store = SessionStore(db, FixedClock())
    session = store.load(session_id)
    session.closed = True
    session.persist()
    before = dump_hashes(db)
    response = client.post(
        f"/api/sessions/{session_id}/proposals/P0001/decision",
        json={"version": 1, "decision": "approve", "execute": False},
    )
    assert response.status_code == 409 and "P02" in response.text
    assert store.load(session_id).proposals[-1].status == "pending"
    assert dump_hashes(db) == before
    db.close()


def test_testclient_self_pipe_exemption_keeps_ordinary_network_blocked() -> None:
    left, right = socket.socketpair()
    left.close()
    right.close()
    for destination in [("127.0.0.1", 1), ("192.0.2.1", 443)]:
        with socket.socket() as connection:
            with pytest.raises(AssertionError, match="must not access the network"):
                connection.connect(destination)
            with pytest.raises(AssertionError, match="must not access the network"):
                connection.connect_ex(destination)

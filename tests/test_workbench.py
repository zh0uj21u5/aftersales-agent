"""End-to-end offline scripts assert durable evidence, never presentation-only fixtures."""

import tomllib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from aftersales.cli import app as cli
from aftersales.core.llm import MockLLM
from aftersales.domain.db import connect, dump_hashes
from aftersales.domain.seed import seed_database
from aftersales.web.app import create_app
from evals.models import load_tasks
from scripts.gen_expectations import generate


@pytest.fixture
def workbench(tmp_path: Path) -> tuple[TestClient, Path, MockLLM]:
    path = tmp_path / "shop.db"
    seed_database(path)
    model = MockLLM([])
    return TestClient(create_app(path, model, trace_dir=tmp_path / "traces")), path, model


def test_static_assets_and_wheel_manifest(workbench: tuple[TestClient, Path, MockLLM]) -> None:
    client, _, model = workbench
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "实时交互审计" in page.text and "评测回放与对比" in page.text
    for asset in ["styles.css", "app.js"]:
        response = client.get("/static/" + asset)
        assert response.status_code == 200 and len(response.content) > 1000
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    wheel = config["tool"]["hatch"]["build"]["targets"]["wheel"]
    for folder in ("static", "samples"):
        assert wheel["force-include"][f"src/aftersales/web/{folder}"] == f"aftersales/web/{folder}"
        assert f"src/aftersales/web/{folder}/**" in wheel["exclude"]
    assert client.get("/static/../app.py").status_code == 404
    assert model.requests == []


@pytest.mark.parametrize("scenario,total", [(1, 4), (2, 3), (3, 2)])
def test_offline_demos_and_restart_evidence(
    workbench: tuple[TestClient, Path, MockLLM], scenario: int, total: int
) -> None:
    client, database, model = workbench
    db = connect(database)
    original = dump_hashes(db)
    initial = client.post(f"/api/demos/{scenario}", json={})
    assert initial.status_code == 201
    state = initial.json()
    session_id = state["session_id"]
    states = []
    for step in range(total):
        response = client.post(f"/api/sessions/{session_id}/demo/advance", json={})
        assert response.status_code == 200, response.text
        state = response.json()
        assert state["demo"]["step"] == step + 1
        states.append(state)
    assert model.requests == [] and dump_hashes(db) == original
    db.close()
    restored = TestClient(create_app(database, MockLLM([]), trace_dir=database.parent / "traces"))
    assert restored.get(f"/api/sessions/{session_id}").json() == state
    for execution in state["action_executions"]:
        proposal = next(
            p for p in state["proposals"] if p["proposal_id"] == execution["proposal_id"]
        )
        action = next(a for a in proposal["actions"] if a["action_id"] == execution["action_id"])
        assert proposal["version"] == execution["version"]
        assert action["canonical_args"] == execution["canonical_args"]
        assert action["preview_amount_cents"] == (
            None
            if execution["tool_name"] == "modify_address"
            else execution["result"]["amount_cents"]
        )
        assert action["status"] == "executed" and proposal["confirm_source"]
        assert execution["args_hash"] and execution["execution_key"]
        assert execution["before_snapshot"] != execution["after_snapshot"]
    denied = {
        rule
        for e in state["trace_events"]
        if e["data"].get("allowed") is False
        for rule in e["data"].get("rule_ids", [])
    }
    if scenario == 1:
        # 13000*2+14000=40000, coupon2000; first share24700; delta3000*2*.95=5700.
        assert states[0]["proposals"][0]["actions"][1]["preview_amount_cents"] == 5700
        assert states[1]["action_executions"] == [] and "P02" in denied
        assert states[2]["proposals"][0]["status"] == "superseded"
        assert state["proposals"][-1]["version"] == 2
        assert [e["tool_name"] for e in state["action_executions"]] == ["modify_address"]
        e = state["action_executions"][0]
        assert e["after_snapshot"]["orders"][0]["address"]["detail"] == "公司88号"
        assert e["after_snapshot"]["refunds"] == []
    elif scenario == 2:
        # (13000-10000)*2*24700/26000 = 5700 cents; retry must keep the original refund ID.
        assert state["action_executions"] == states[1]["action_executions"]
        e = state["action_executions"][0]
        assert e["result"]["amount_cents"] == 5700
        assert len(e["after_snapshot"]["refunds"]) == 1
        assert e["before_snapshot"]["orders"][0]["price_protected"] == 0
        assert e["after_snapshot"]["orders"][0]["price_protected"] == 1
        assert "P02" in denied
        recovered = [e["data"] for e in state["trace_events"] if e["event"] == "recovery"]
        assert recovered[-1]["refunds_count"] == 1 and recovered[-1]["result"] == e["result"]
    else:
        # 3*15000=45000 >20000: approval only, no business refund.
        a = states[0]["proposals"][0]["actions"][0]
        assert a["preview_amount_cents"] == 45000 and a["requires_approval"]
        assert {"P01", "P08"} <= denied
        assert state["action_executions"] == []
        assert (
            state["approvals"][0]["id"] == "AP0001" and state["approvals"][0]["status"] == "pending"
        )
        assert next(o for o in state["orders"] if o["orders"][0]["id"] == "O1005")["refunds"] == []
    # Finished scripts are inert, and another initialization gets an independent database.
    assert (
        client.post(f"/api/sessions/{session_id}/demo/advance", json={}).json()["action_executions"]
        == state["action_executions"]
    )
    assert client.post(f"/api/demos/{scenario}", json={}).json()["action_executions"] == []


def test_demo_invalid_and_non_demo(workbench: tuple[TestClient, Path, MockLLM]) -> None:
    client, _, _ = workbench
    assert client.post("/api/demos/4", json={}).status_code == 404
    session_id = client.post("/api/sessions", json={}).json()["session_id"]
    assert client.post(f"/api/sessions/{session_id}/demo/advance", json={}).status_code == 409


def test_v2_gold_and_generated_amounts() -> None:
    tasks = load_tasks("evals/tasks_v2")
    assert len(tasks) == 8 and sum("normal_control" in t.tags for t in tasks) == 2
    assert generate(Path("evals/tasks_v2"), check=True) == []


def test_modify_card_message_generates_v2_without_model_or_implicit_consent(
    workbench: tuple[TestClient, Path, MockLLM],
) -> None:
    client, _, model = workbench
    session_id = client.post("/api/demos/1", json={}).json()["session_id"]
    client.post(f"/api/sessions/{session_id}/demo/advance", json={})
    response = client.post(
        f"/api/sessions/{session_id}/messages", json={"content": "只改地址，先不退差"}
    )
    assert response.status_code == 200, response.text
    state = response.json()["session"]
    assert state["proposals"][0]["status"] == "superseded"
    assert state["proposals"][-1]["version"] == 2 and state["proposals"][-1]["status"] == "pending"
    assert state["action_executions"] == [] and model.requests == []
    url = f"/api/sessions/{session_id}/proposals/P0002/decision"
    approved = client.post(url, json={"version": 2, "decision": "approve"}).json()
    assert [e["tool_name"] for e in approved["action_executions"]] == ["modify_address"]
    assert (
        client.post(f"/api/sessions/{session_id}/demo/advance", json={}).json()["action_executions"]
        == approved["action_executions"]
    )


def test_card_then_script_replays_one_refund(workbench: tuple[TestClient, Path, MockLLM]) -> None:
    # O1006 price delta 3000*2*(24700/26000)=5700; button and script share one action key.
    client, _, _ = workbench
    session_id = client.post("/api/demos/2", json={}).json()["session_id"]
    client.post(f"/api/sessions/{session_id}/demo/advance", json={})
    response = client.post(
        f"/api/sessions/{session_id}/proposals/P0001/decision",
        json={"version": 1, "decision": "approve"},
    )
    records = response.json()["action_executions"]
    for _ in range(2):
        result = client.post(f"/api/sessions/{session_id}/demo/advance", json={})
        assert result.status_code == 200, result.text
        assert result.json()["action_executions"] == records


def test_serve_offline_does_not_load_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "offline.db"
    seed_database(path)

    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Offline serve must not load credentials or create a provider client")

    def launch(application: Any, *, host: str, port: int) -> None:
        assert (host, port) == ("127.0.0.1", 8000)
        client = TestClient(application)
        session_id = client.post("/api/sessions", json={}).json()["session_id"]
        reply = client.post(f"/api/sessions/{session_id}/messages", json={"content": "随便聊聊"})
        assert reply.status_code == 200 and "纯离线模式" in reply.json()["turn"]["reply"]
        assert reply.json()["session"]["action_executions"] == []

    monkeypatch.setattr("aftersales.cli.load_settings", forbidden)
    monkeypatch.setattr("aftersales.cli.make_client", forbidden)
    monkeypatch.setattr("uvicorn.run", launch)
    result = CliRunner().invoke(cli, ["serve", "--offline", "--db", str(path)])
    assert result.exit_code == 0, result.output

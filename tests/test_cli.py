import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aftersales import cli
from aftersales.cli import app
from aftersales.core.config import LLMSettings
from aftersales.core.llm import LLMResponse, MockLLM, ToolCall
from aftersales.core.tracing import read_trace
from aftersales.domain.seed import seed_database


def test_cli_help() -> None:
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("chat", "eval", "trace", "seed"):
        assert cmd in result.output


def test_chat_and_trace_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "shop.db"
    seed_database(db)
    monkeypatch.setattr(
        cli, "load_settings", lambda: LLMSettings(provider="deepseek", model="mock", api_key="test")
    )
    monkeypatch.setattr(
        cli, "make_client", lambda *args: MockLLM([LLMResponse(text="请提供手机号和姓名")])
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["chat", "--db", str(db), "--trace-dir", str(tmp_path)], input="你好\n/exit\n"
    )
    assert result.exit_code == 0, result.output
    assert "请提供手机号和姓名" in result.output
    path = next(tmp_path.glob("*.jsonl"))
    assert read_trace(path)[-1].event == "run_end"
    rendered = runner.invoke(app, ["trace", path.stem, "--trace-dir", str(tmp_path)])
    assert rendered.exit_code == 0 and "assistant_msg" in rendered.output


def test_chat_missing_database_and_trace_errors(tmp_path: Path) -> None:
    runner = CliRunner()
    assert runner.invoke(app, ["chat", "--db", str(tmp_path / "missing.db")]).exit_code != 0
    assert not (tmp_path / "missing.db").exists()
    for run_id in ("missing", "../private"):
        assert runner.invoke(app, ["trace", run_id, "--trace-dir", str(tmp_path)]).exit_code != 0


def test_chat_displays_stored_card_until_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # O1006 price protection: (13000 - 10000) * 2 * 24700 / 26000 = 5700 cents.
    db = tmp_path / "shop.db"
    seed_database(db)
    monkeypatch.setattr(
        cli, "load_settings", lambda: LLMSettings(provider="deepseek", model="mock", api_key="test")
    )
    price = ToolCall(
        id="write",
        name="apply_price_protection",
        arguments={"order_id": "O1006", "order_item_id": "OI00006"},
    )
    propose = ToolCall(
        id="plan",
        name="propose_actions",
        arguments={"actions": [{"tool_name": price.name, "arguments": price.arguments}]},
    )
    auth = ToolCall(
        id="auth",
        name="find_user_by_order",
        arguments={"order_id": "O1006", "receiver_name": "用户006"},
    )
    mock = MockLLM(
        [
            LLMResponse(tool_calls=[auth, propose], stop_reason="tool_use"),
            LLMResponse(text="请确认方案。"),
            LLMResponse(text="请再次核对。"),
            LLMResponse(tool_calls=[price], stop_reason="tool_use"),
            LLMResponse(text="已完成。"),
        ]
    )
    monkeypatch.setattr(cli, "make_client", lambda *args: mock)
    result = CliRunner().invoke(
        app,
        [
            "chat",
            "--db",
            str(db),
            "--config",
            "configs/proposal.yaml",
            "--trace-dir",
            str(tmp_path),
        ],
        input="申请价保\n请再展示\n好的，请帮我办理\n/exit\n",
    )
    assert result.exit_code == 0, result.output
    for value in (
        "服务端待确认方案",
        "P0001 · v1",
        "ACT01",
        "价保退差",
        "O1006",
        "OI00006",
        "退款 ¥57.00",
    ):
        assert result.output.count(value) == 2, result.output
    assert "服务端待确认方案" not in result.output.split("已完成。")[-1]


def test_windows_redirected_output_supports_yen(tmp_path: Path) -> None:
    from aftersales.core.tracing import TraceWriter
    from aftersales.domain.clock import FixedClock

    writer = TraceWriter(FixedClock(), tmp_path, "encoding")
    writer.emit("assistant_msg", content="退款 ¥57.00")
    writer.close()
    result = subprocess.run(
        [sys.executable, "-m", "aftersales.cli", "trace", "encoding", "--trace-dir", str(tmp_path)],
        env={**os.environ, "PYTHONIOENCODING": "gbk"},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "退款 ¥57.00" in result.stdout.decode("utf-8")

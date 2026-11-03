import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aftersales import cli
from aftersales.cli import app
from aftersales.core.config import LLMSettings
from aftersales.core.llm import LLMResponse, MockLLM
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

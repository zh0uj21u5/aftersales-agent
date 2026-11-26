import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest
from openai.types.chat import ChatCompletion

from scripts import run_step_b


def test_budget_retains_unknown_attempt_before_refusing_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_step_b, "CEILING", Decimal("0.002"))
    completion = ChatCompletion.model_validate(
        {
            "id": "response",
            "choices": [],
            "created": 0,
            "model": run_step_b.MODEL,
            "object": "chat.completion",
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        }
    )
    send = Mock(side_effect=[completion, TimeoutError()])
    budget = run_step_b.Budget(send, tmp_path / "requests.jsonl")
    payload = {"model": run_step_b.MODEL, "messages": [], "max_tokens": 16}
    budget.create(**payload)
    assert budget.known == Decimal("0.000042")
    with pytest.raises(TimeoutError):
        budget.create(**payload)
    with pytest.raises(run_step_b.BudgetExceeded):
        budget.create(**payload)
    assert send.call_count == 2 and budget.unknown > 0
    events = [json.loads(line)["event"] for line in budget.ledger.read_text().splitlines()]
    assert events == ["reserve", "metered", "reserve", "unknown", "denied"]

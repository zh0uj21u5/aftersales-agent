"""Run the one-shot, sixteen-session M7 experiment with a shared USD reference budget."""

import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aftersales.core.config import load_settings
from aftersales.core.llm import (
    LLMClient,
    LLMRequest,
    LLMResponse,
    Message,
    Usage,
    estimate_cost,
    make_client,
)
from aftersales.core.tracing import read_trace
from aftersales.tools.registry import ToolSpec
from evals.models import load_tasks
from evals.proposal_metrics import analyze
from evals.runner import TrialResult, run_suite
from scripts.gen_expectations import generate

ROOT = Path("evals/reports/m7")
TRACE = Path("traces/m7")


class BudgetClient:
    """Reserve a conservative request envelope before contacting the provider."""

    def __init__(self, inner: LLMClient, model: str) -> None:
        self.inner, self.model = inner, model
        self.charged = Decimal(0)
        self.unknown_reserve = Decimal(0)

    def chat(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        temperature: float = 0.0,
        max_tokens: int = 2048,
    ) -> LLMResponse:
        request = LLMRequest(
            system=system,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        # UTF-8 byte envelope plus framing allowance; three attempts include adapter retries.
        reserve = estimate_cost(
            self.model,
            Usage(
                input_tokens=len(request.model_dump_json().encode()) + 4096,
                output_tokens=max_tokens,
            ),
        )
        if reserve is None or self.charged + self.unknown_reserve + reserve * 3 > Decimal("0.95"):
            raise RuntimeError("M7 reference budget exhausted; no new API request permitted")
        self.unknown_reserve += reserve * 3
        response = self.inner.chat(system, messages, tools, temperature, max_tokens)
        cost = estimate_cost(self.model, response.usage)
        if response.stop_reason != "error" and cost is not None:
            self.unknown_reserve -= reserve * 3
            self.charged += cost
        return response


def main() -> None:
    settings = load_settings()
    if settings.model != "deepseek-v4.1-flash":
        raise ValueError("Model changed since ADR-029; revise the estimate before running")
    if generate(Path("evals/tasks"), check=True) or generate(Path("evals/tasks_v2"), check=True):
        raise ValueError("Expectations changed")
    tasks = load_tasks("evals/tasks_v2")
    if len(tasks) != 8:
        raise ValueError("Require exactly eight tasks")
    ROOT.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents a second invocation, including after a crash.
    with (ROOT / "started.json").open("x", encoding="utf-8") as stream:
        json.dump({"model": settings.model, "session_limit": 16, "budget_usd": "1.00"}, stream)
    client = BudgetClient(make_client(settings.provider, settings.model), settings.model)
    supplementary = []
    sessions = 0

    def completed(result: TrialResult) -> None:
        nonlocal sessions
        sessions += 1
        with (ROOT / "completed.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(result.model_dump_json(by_alias=True) + "\n")
        print(
            f"session={sessions}/16 task={result.task_id} pass={result.grade.passed} reference_usd={client.charged}",
            flush=True,
        )

    for label, enabled in [("A", False), ("B", True)]:
        config = settings.agent_config().model_copy(
            update={
                "guard": True,
                "confirm_gate": True,
                "proposal_gate": enabled,
                "max_steps": 8,
                "max_tokens": 1536,
            }
        )
        report = run_suite(
            tasks,
            client,
            config,
            n=1,
            k=1,
            trace_dir=TRACE,
            report_dir=Path("evals/reports"),
            progress=completed,
            user_mode="scripted",
        )
        per_task = {
            t.task_id: analyze(task, read_trace(TRACE / f"{t.trace_id}.jsonl")).model_dump()
            for task, t in zip(tasks, report.trials, strict=True)
        }
        supplementary.append(
            {
                "group": label,
                "run_id": report.run_id,
                "metrics": report.metrics,
                "task_metrics": per_task,
            }
        )
    payload = {
        "sessions": sessions,
        "reference_cost_usd": str(client.charged),
        "unknown_reserve_usd": str(client.unknown_reserve),
        "groups": supplementary,
        "frozen_tasks_sha256": hashlib.sha256(
            b"".join(p.read_bytes() for p in sorted(Path("evals/tasks_v2").glob("*.yaml")))
        ).hexdigest(),
    }
    (ROOT / "comparison.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in payload.items() if k != "groups"}), flush=True)


if __name__ == "__main__":
    main()

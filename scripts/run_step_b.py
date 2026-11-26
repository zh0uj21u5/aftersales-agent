"""Run the approved full benchmark once with a shared per-request spend ceiling."""

import argparse
import json
import subprocess
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from threading import Condition
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import openai
import yaml
from openai.types.chat import ChatCompletion

from aftersales.core.config import AgentConfig, load_settings
from aftersales.core.llm import OpenAICompatClient, Usage, estimate_cost
from evals.models import load_tasks
from evals.runner import TrialResult, run_suite

MODEL = "deepseek-v4.1-flash"
CEILING = Decimal("12.00")
LEDGER = Path("data/step-b")
REPORTS = Path("evals/reports/step-b")
TRACES = Path("traces/step-b")
CONFIGS = {"A": "baseline", "B": "proposal"}


class BudgetExceeded(RuntimeError):
    """The request reservation would exceed the approved reference spend."""


class Budget:
    """Account for every HTTP attempt, including SDK calls made during adapter retries."""

    def __init__(
        self, send: Callable[..., ChatCompletion], ledger: Path, *, limit: Decimal | None = None
    ) -> None:
        self.send, self.ledger = send, ledger
        self.limit = CEILING if limit is None else limit
        self.known = self.unknown = self.inflight = Decimal(0)
        self.attempts = self.denied = 0
        self.condition = Condition()
        self.group = "A"

    def record(self, attempt: int, event: str, **values: Any) -> None:
        with self.ledger.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(dict(group=self.group, attempt=attempt, event=event, **values)) + "\n"
            )

    def create(self, **payload: Any) -> ChatCompletion:
        if payload["model"] != MODEL:
            raise ValueError("Unexpected model in budgeted request")
        envelope = estimate_cost(
            MODEL,
            Usage(
                input_tokens=len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + 4096,
                output_tokens=payload["max_tokens"],
            ),
        )
        assert envelope is not None
        with self.condition:
            while (
                self.known + self.unknown + self.inflight + envelope > self.limit and self.inflight
            ):
                self.condition.wait()
            if self.known + self.unknown + envelope > self.limit:
                self.denied += 1
                self.record(0, "denied", reserve_usd=str(envelope))
                raise BudgetExceeded(f"USD {self.limit} reference budget reached")
            self.attempts += 1
            attempt = self.attempts
            self.record(attempt, "reserve", reserve_usd=str(envelope))
            self.inflight += envelope

        outcome, known, unknown = "unknown", Decimal(0), envelope
        details: dict[str, Any] = {}
        try:
            response = self.send(**payload)
            details["response_id"] = response.id
            if response.usage is not None:
                usage = Usage(
                    input_tokens=response.usage.prompt_tokens,
                    output_tokens=response.usage.completion_tokens,
                )
                known = estimate_cost(MODEL, usage)
                assert known is not None
                outcome, unknown = "metered", Decimal(0)
                details["tokens"] = usage.model_dump()
            return response
        except openai.APIStatusError as error:
            details["status_code"] = error.status_code
            if 400 <= error.status_code < 500 and error.status_code != 408:
                outcome, unknown = "rejected", Decimal(0)
            raise
        except Exception as error:
            details["error_type"] = type(error).__name__
            raise
        finally:
            with self.condition:
                self.inflight -= envelope
                self.known += known
                self.unknown += unknown
                self.record(
                    attempt, outcome, known_usd=str(known), unknown_usd=str(unknown), **details
                )
                self.condition.notify_all()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", default=12, type=int)
    args = parser.parse_args()
    if not 1 <= args.workers <= 12:
        raise ValueError("workers must be in 1..12")
    settings = load_settings()
    endpoint = urlsplit(settings.base_url or "")
    if (
        settings.model != MODEL
        or endpoint.scheme != "https"
        or not (endpoint.hostname or "").endswith(".cn-beijing.maas.aliyuncs.com")
    ):
        raise ValueError("Configured model or billing region differs from the approved experiment")
    tasks = load_tasks(Path("evals/tasks"))
    if [task.id[:4] for task in tasks] != [f"t{i:03d}" for i in range(1, 61)]:
        raise ValueError("Require the exact t001 through t060 task set")
    configs = {
        group: AgentConfig.model_validate(
            settings.agent_config().model_dump()
            | yaml.safe_load(Path(f"configs/{name}.yaml").read_text(encoding="utf-8"))
        )
        for group, name in CONFIGS.items()
    }
    a, b = (config.model_dump() for config in configs.values())
    toggles = {"guard", "confirm_gate", "proposal_gate", "preview_tool"}
    if (
        {key for key in a if a[key] != b[key]} != toggles
        or any(a[key] or not b[key] for key in toggles)
        or a["policy_mode"] != "full"
        or a["todo"]
        or a["goal_judge"]
    ):
        raise ValueError("A/B presets differ from the approved intervention")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", "src", "evals", "configs", __file__],
        check=True,
    )
    LEDGER.mkdir(parents=True, exist_ok=True)
    with (LEDGER / "started.json").open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "revision": revision,
                "model": MODEL,
                "budget_usd": str(CEILING),
                "sessions": 360,
                "workers": args.workers,
            },
            stream,
        )
    with openai.OpenAI(
        api_key=settings.api_key.get_secret_value(),
        base_url=settings.base_url,
        max_retries=0,
        timeout=60.0,
    ) as sdk:
        budget = Budget(sdk.chat.completions.create, LEDGER / "requests.jsonl")
        llm = OpenAICompatClient(
            MODEL,
            settings.api_key.get_secret_value(),
            settings.base_url,
            sdk=SimpleNamespace(chat=SimpleNamespace(completions=budget)),
        )
        completed = 0
        for group, config in configs.items():
            budget.group = group
            with (LEDGER / f"{group}-trials.jsonl").open(
                "x", encoding="utf-8", buffering=1
            ) as journal:

                def checkpoint(trial: TrialResult, label: str = group) -> None:
                    nonlocal completed
                    journal.write(trial.model_dump_json(by_alias=True) + "\n")
                    completed += 1
                    print(
                        f"{label} {completed}/360 {trial.task_id} trial={trial.trial} "
                        f"pass={trial.grade.passed} stop={trial.stop_reason} known_usd={budget.known} "
                        f"unknown_reserve_usd={budget.unknown} budget_denied={budget.denied}",
                        flush=True,
                    )

                report = run_suite(
                    tasks,
                    llm,
                    config,
                    k=3,
                    n=3,
                    user_mode="llm",
                    simulator_llm=llm,
                    simulator_model=MODEL,
                    trace_dir=TRACES / group,
                    report_dir=REPORTS / group,
                    progress=checkpoint,
                    workers=args.workers,
                )
            print(
                f"{group} report={report.run_id} source_sha256={report.source_sha256}", flush=True
            )
        (LEDGER / "finished.json").write_text(
            json.dumps(
                {
                    "known_usd": str(budget.known),
                    "unknown_reserve_usd": str(budget.unknown),
                    "attempts": budget.attempts,
                    "denied": budget.denied,
                    "completed": completed,
                }
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()

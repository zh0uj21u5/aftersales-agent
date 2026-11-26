"""Run the approved C comparison exactly once under its shared reference cap."""

import argparse
import hashlib
import json
import subprocess
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import openai
import yaml

from aftersales.core.config import AgentConfig, load_settings
from aftersales.core.llm import OpenAICompatClient
from evals.models import load_tasks
from evals.runner import EvalReport, TrialResult, run_suite
from scripts.run_step_b import MODEL, Budget

CEILING = Decimal("7.50")
SESSIONS = 180
LEDGER = Path("data/step-c-2")
REPORTS = Path("evals/reports/step-c/C")
TRACES = Path("traces/step-c/C")
CONFIG = Path("configs/proposal_llm.yaml")
B_REPORT = Path("evals/reports/step-b/B/afc9c225eb0d4b23bd1ebf51695fcf44.json")
B_REPORT_SHA256 = "357425c1d562047ccfd466ba0946d25e917167ee724b94c3ec80a59bd538be53"


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
    task_sha = hashlib.sha256(
        json.dumps(
            [task.model_dump() for task in tasks], sort_keys=True, ensure_ascii=False
        ).encode()
    ).hexdigest()
    frozen_b = B_REPORT.read_bytes()
    if hashlib.sha256(frozen_b).hexdigest() != B_REPORT_SHA256:
        raise ValueError("Archived B report differs from the approved comparator")
    comparator = EvalReport.model_validate_json(frozen_b)
    config = AgentConfig.model_validate(
        settings.agent_config().model_dump() | yaml.safe_load(CONFIG.read_bytes())
    )
    if (
        task_sha != comparator.dataset_sha256
        or comparator.config.confirm_mode != "heuristic"
        or config != comparator.config.model_copy(update={"confirm_mode": "llm"})
        or comparator.simulator_model != MODEL
        or comparator.user_mode != "llm"
        or comparator.seed != 42
        or comparator.k != 3
        or comparator.trials_per_task != 3
    ):
        raise ValueError("Expected the B task set and presets with only confirm_mode changed")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    subprocess.run(
        ["git", "diff", "--quiet", "HEAD", "--", "src", "evals", "configs", __file__],
        check=True,
    )
    if any(REPORTS.glob("*.json")) or any(TRACES.glob("*.jsonl")):
        raise FileExistsError("C reports or traces already exist")
    LEDGER.mkdir(parents=True, exist_ok=True)
    with (LEDGER / "started.json").open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "revision": revision,
                "model": MODEL,
                "budget_usd": str(CEILING),
                "sessions": SESSIONS,
                "workers": args.workers,
                "dataset_sha256": task_sha,
                "b_report_sha256": B_REPORT_SHA256,
            },
            stream,
        )
    with (
        openai.OpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=settings.base_url,
            max_retries=0,
            timeout=60.0,
        ) as sdk,
        (LEDGER / "C-trials.jsonl").open("x", encoding="utf-8", buffering=1) as journal,
    ):
        budget = Budget(sdk.chat.completions.create, LEDGER / "requests.jsonl", limit=CEILING)
        budget.group = "C"
        client = OpenAICompatClient(
            MODEL,
            settings.api_key.get_secret_value(),
            settings.base_url,
            sdk=SimpleNamespace(chat=SimpleNamespace(completions=budget)),
        )
        completed = 0

        def checkpoint(trial: TrialResult) -> None:
            nonlocal completed
            journal.write(trial.model_dump_json(by_alias=True) + "\n")
            completed += 1
            print(
                f"C {completed}/{SESSIONS} {trial.task_id} trial={trial.trial} "
                f"pass={trial.grade.passed} stop={trial.stop_reason} known_usd={budget.known} "
                f"unknown_reserve_usd={budget.unknown} budget_denied={budget.denied}",
                flush=True,
            )

        report = run_suite(
            tasks,
            client,
            config,
            k=3,
            n=3,
            seed=42,
            user_mode="llm",
            simulator_llm=client,
            simulator_model=MODEL,
            trace_dir=TRACES,
            report_dir=REPORTS,
            progress=checkpoint,
            workers=args.workers,
        )
        (LEDGER / "finished.json").write_text(
            json.dumps(
                {
                    "run_id": report.run_id,
                    "known_usd": str(budget.known),
                    "unknown_reserve_usd": str(budget.unknown),
                    "attempts": budget.attempts,
                    "denied": budget.denied,
                    "completed": completed,
                }
            ),
            encoding="utf-8",
        )
        print(f"C report={report.run_id} source_sha256={report.source_sha256}", flush=True)


if __name__ == "__main__":
    main()

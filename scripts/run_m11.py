"""Run the 18 approved drift trials once under a shared USD 0.50 request cap."""

import hashlib
import json
import subprocess
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import openai
import yaml

from aftersales.core.config import AgentConfig, load_settings
from aftersales.core.llm import OpenAICompatClient
from evals.models import load_tasks
from evals.runner import TrialResult, dataset_sha256, run_suite
from scripts.run_step_b import MODEL, Budget

CEILING = Decimal("0.50")
K = 3
WORKERS = 4
LEDGER = Path("data/m11")
REPORTS = Path("evals/reports/m11")
TRACES = Path("traces/m11")
TASKS = Path("evals/tasks_drift")
CONFIG = Path("configs/output_guard.yaml")
HISTORICAL_SHA256 = "98dad7a1c8a9b0dea033e58dc34c213f71ab845b0d3aaf7570fc4b5594c16a1d"


def main() -> None:
    settings = load_settings()
    endpoint = urlsplit(settings.base_url or "")
    if (
        settings.model != MODEL
        or settings.provider == "anthropic"
        or endpoint.scheme != "https"
        or not (endpoint.hostname or "").endswith(".cn-beijing.maas.aliyuncs.com")
    ):
        raise ValueError("Configured model or billing region differs from the approved experiment")

    tasks = load_tasks(TASKS)
    if [task.id[:3] for task in tasks] != [f"d{i:02d}" for i in range(1, 7)]:
        raise ValueError("Require the six drift tasks d01 through d06")
    if dataset_sha256(load_tasks(Path("evals/tasks"))) != HISTORICAL_SHA256:
        raise ValueError("Historical 60-task dataset has changed")
    config_bytes = CONFIG.read_bytes()
    preset = yaml.safe_load(Path("configs/proposal_llm.yaml").read_bytes()) | {"output_guard": True}
    if yaml.safe_load(config_bytes) != preset:
        raise ValueError("Expected the proposal_llm preset with only output_guard added")
    config = AgentConfig.model_validate(settings.agent_config().model_dump() | preset)

    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    frozen = (
        "src",
        "evals",
        "configs",
        "pyproject.toml",
        "scripts/run_m11.py",
        "scripts/run_step_b.py",
    )
    if subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *frozen], text=True
    ):
        raise ValueError("Commit the evaluation source, tasks and config before launching")
    if any(REPORTS.glob("*")) or any(TRACES.glob("*")):
        raise FileExistsError("M11 reports or traces already exist")
    LEDGER.mkdir(parents=True, exist_ok=True)
    sessions = len(tasks) * K
    with (LEDGER / "started.json").open("x", encoding="utf-8") as stream:
        json.dump(
            {
                "revision": revision,
                "model": MODEL,
                "budget_usd": str(CEILING),
                "user_mode": "llm",
                "k": K,
                "n": K,
                "sessions": sessions,
                "workers": WORKERS,
                "dataset_sha256": dataset_sha256(tasks),
                "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
                "local_cache": False,
            },
            stream,
        )

    with (
        openai.OpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=settings.base_url,
            max_retries=0,
            timeout=60.0,
            http_client=httpx.Client(follow_redirects=False, timeout=60.0),
        ) as sdk,
        (LEDGER / "trials.jsonl").open("x", encoding="utf-8", buffering=1) as journal,
    ):
        budget = Budget(sdk.chat.completions.create, LEDGER / "requests.jsonl", limit=CEILING)
        budget.group = "M11"
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
                f"M11 {completed}/{sessions} {trial.task_id} trial={trial.trial} "
                f"pass={trial.grade.passed} stop={trial.stop_reason} known_usd={budget.known} "
                f"unknown_reserve_usd={budget.unknown} budget_denied={budget.denied}",
                flush=True,
            )

        report = run_suite(
            tasks,
            client,
            config,
            k=K,
            n=K,
            seed=42,
            user_mode="llm",
            simulator_model=MODEL,
            trace_dir=TRACES,
            report_dir=REPORTS,
            cache_dir=None,
            progress=checkpoint,
            workers=WORKERS,
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
        print(f"M11 report={report.run_id} source_sha256={report.source_sha256}", flush=True)


if __name__ == "__main__":
    main()

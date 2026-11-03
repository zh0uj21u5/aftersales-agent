"""Generate or verify YAML amounts exclusively through strict Services replay."""

import argparse
import sys
from pathlib import Path

import yaml

# Permit both `python scripts/gen_expectations.py` and `python -m scripts.gen_expectations`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aftersales.domain.clock import FixedClock
from aftersales.domain.services import Services
from evals.models import EvalTask
from evals.replay import expected_mentions, fresh_database


def generate(directory: Path, *, check: bool = False, seed: int = 42) -> list[str]:
    changed = []
    paths = sorted(directory.glob("*.yaml"))
    if not paths:
        raise ValueError("没有评测任务")
    for path in paths:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        task = EvalTask.model_validate(raw)
        db = fresh_database(seed)
        try:
            mentions = expected_mentions(Services(db, FixedClock()), task)
        finally:
            db.close()
        if mentions != task.must_mention:
            changed.append(task.id)
            if not check:
                raw["must_mention"] = mentions
                path.write_text(
                    yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
                )
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=Path("evals/tasks"))
    parser.add_argument("--check", action="store_true")
    options = parser.parse_args()
    changed = generate(options.tasks, check=options.check)
    if options.check and changed:
        parser.exit(1, "金额期望不一致：" + ", ".join(changed) + "\n")
    print(
        f"Services 重放与金额{'校验' if options.check else '生成'}通过；变更 {len(changed)} 条任务"
    )


if __name__ == "__main__":
    main()

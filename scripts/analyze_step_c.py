"""Reconcile the C run and render a three-column comparison from archived evidence."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from evals.models import load_tasks
from evals.replay import fresh_database
from evals.runner import EvalReport, TrialResult, aggregate, percentile, report_markdown
from scripts import run_step_b as historical
from scripts import run_step_c as current
from scripts.analyze_step_b import (
    PURPOSES,
    STRATA,
    Evidence,
    check_trial,
    ratio,
    reconcile_injection_scope,
    sha256,
    task_number,
    total,
    verify_ledger,
)

FROZEN_SOURCE = "95089a68e1d52a9248a4303785c220344839febe2c7416132a6503a400181e32"
FROZEN_REPORTS = {
    "A": "305f98c8c1e2bd7a2b6d076e2563d02aaa339d79acb8597d2ac6306c1f7e548c",
    "B": current.B_REPORT_SHA256,
}
FROZEN_MARKDOWN = {
    "A": "2567cbf9b0cebcb8014d11ea9f1ad288a478fada68d3f77557b374c8340b04e7",
    "B": "7c6fc69d31f2afe337451f6fc4a297bf3442c588479519c09b3eed4ba3477617",
}
FROZEN_TRACES = {
    "A": "4ec28e42a50fa420724b2a5e3cd110777e354dd25e3ab88a569bdcad33f118ae",
    "B": "c83214775a2cb17172abae2b545196a7acf84b4e0ed64dac9165da971de52b5d",
}
GROUPS = ("A", "B", "C")
REPRESENTATIVE = {
    1: "改址 · 方案编号",
    4: "待支付取消 · 商品金额",
    5: "取消退券 · 商品合计与抵扣",
    49: "取消退券后用券下单",
    55: "大额审批后新购",
}


def table_row(*values: Any) -> str:
    return "| " + " | ".join(str(v) for v in values) + " |"


def comparison_header(title: str) -> list[str]:
    return [
        table_row(title, "A · baseline", "B · proposal (heuristic)", "C · proposal_llm"),
        "|---|---:|---:|---:|",
    ]


def stratum_cell(trials: list[TrialResult]) -> str:
    metrics, _ = aggregate(trials, 3)
    rate = [ratio(metrics[k]) for k in ("pass^1", "pass^3", "state_accuracy", "amount_accuracy")]
    injection = metrics["injection_trials"]
    if injection:
        successes = sum(any(v.injected for v in t.grade.violations) for t in trials)
        rate.append(f"{successes}/{injection}")
    else:
        rate.append("—")
    return " / ".join(rate)


def render(reports: dict[str, EvalReport], evidence: dict[str, Evidence]) -> str:
    results = [reports[group] for group in GROUPS]
    audits = [evidence[group] for group in GROUPS]
    rows = comparison_header("指标")
    rows.append(
        table_row("通过会话", *(f"{sum(t.grade.passed for t in r.trials)}/180" for r in results))
    )
    for key, name in {
        "pass^1": "pass^1",
        "pass^3": "pass^3",
        "state_accuracy": "数据库终态正确率",
        "amount_accuracy": "金额准确率",
        "injection_success_rate": "正式影子注入成功率（全标签）",
    }.items():
        rows.append(table_row(name, *(ratio(r.metrics[key]) for r in results)))
    rows.append(table_row("金额断言会话", *(r.metrics["amount_trials"] for r in results)))
    for key, name in {
        "unauthorized_reads": "成功越权读取（影子 P01）",
        "violating_writes": "成功违规写入（影子非 P02）",
        "unauthorized_writes": "成功未授权写入（影子 P01、P02 或注入夹带）",
        "injected_actions": "正式影子注入成功调用",
        "normalized_extra_writes_in_injection": "注入题的规范化 Gold 范围外成功写入",
        "guard_blocks": "Guard 实际拦截",
        "confirmation_blocks": "确认门实际拦截",
        "service_blocks": "Services 实际拦截",
        "trials_with_confirmation_block": "含确认门拦截的会话",
        "failed_trials_with_confirmation_block": "含确认门拦截且失败的会话",
    }.items():
        rows.append(table_row(name, *(a.counts[key] for a in audits)))
    rows.append(
        table_row("成功写入中的影子 P02", *(r.metrics["unconfirmed_writes"] for r in results))
    )
    rows.append(
        table_row(
            "注入题中实际 P01 或规范化 Gold 范围外写入会话",
            *(
                f"{len(a.normalized_injections)}/{r.metrics['injection_trials']}"
                for a, r in zip(audits, results, strict=True)
            ),
        )
    )
    for kind, name in (("direct", "直接注入"), ("indirect", "间接注入")):
        rows.append(
            table_row(
                f"{name}已暴露会话",
                *(
                    f"{r.metrics[f'injection_{kind}_exposed_trials']}/{r.metrics[f'injection_{kind}_trials']}"
                    for r in results
                ),
            )
        )
    rows.extend(
        [
            "",
            "### 12 个标签分层",
            "",
            "每格依次为 pass^1 / pass^3 / 终态正确率 / 金额准确率 / 正式影子注入成功会话。",
            "",
            "| 标签 | 题/组 | A · baseline | B · proposal (heuristic) | C · proposal_llm |",
            "|---|---:|---|---|---|",
        ]
    )
    for name, tags in STRATA.items():
        subsets = [
            [
                t
                for t in r.trials
                if tags.intersection(t.tags) or (not tags and 29 <= task_number(t.task_id) <= 40)
            ]
            for r in results
        ]
        rows.append(table_row(name, len(subsets[0]) // 3, *(stratum_cell(s) for s in subsets)))

    rows.extend(["", "### 三方用量、费用与延迟", "", *comparison_header("指标")])
    for purpose, label in zip(PURPOSES, ("主 Agent", "确认分类器", "买家模拟器"), strict=True):
        for direction in ("input", "output"):
            counts = [getattr(a.tokens[purpose], f"{direction}_tokens") for a in audits]
            rows.append(
                table_row(
                    f"{label} {'输入' if direction == 'input' else '输出'} token",
                    *(f"{v:,}（均值 {v / 180:,.1f}）" for v in counts),
                )
            )
        rows.append(table_row(f"{label} 已计量参考 USD", *(a.cost[purpose] for a in audits)))
        rows.append(table_row(f"{label} 调用数", *(len(a.latencies[purpose]) for a in audits)))
        for percent in (50, 95):
            rows.append(
                table_row(
                    f"{label} 调用 P{percent}（ms）",
                    *(percentile(a.latencies[purpose], percent) or "—" for a in audits),
                )
            )
    for key, name in {
        "task_latency_p50_ms": "会话延迟 P50（ms）",
        "task_latency_p95_ms": "会话延迟 P95（ms）",
        "llm_latency_p50_ms": "所有调用延迟 P50（ms）",
        "llm_latency_p95_ms": "所有调用延迟 P95（ms）",
        "turns_p50": "会话轮数 P50",
        "turns_p95": "会话轮数 P95",
        "unknown_cost_trials": "正式报告中费用未知的会话",
        "cache_hits": "本地缓存命中",
    }.items():
        rows.append(table_row(name, *(r.metrics[key] for r in results)))
    rows.append(table_row("每会话平均轮数", *(f"{r.metrics['turns_mean']:.2f}" for r in results)))
    rows.append(table_row("总对话轮数", *(sum(t.turns for t in r.trials) for r in results)))
    rows.append(table_row("总已计量参考 USD", *(total(a.cost.values()) for a in audits)))
    rows.append(
        table_row("每会话已计量参考 USD", *(f"{total(a.cost.values()) / 180:.6f}" for a in audits))
    )
    for key in (
        "http_metered",
        "http_rejected",
        "http_unknown",
        "budget_denied",
        "agent_truncated",
        "confirmation_truncated",
        "simulator_truncated",
        "classifier/APPROVE",
        "classifier/REJECT",
        "classifier/NEUTRAL",
        "classifier/ERROR",
    ):
        rows.append(table_row(key, *(a.counts[key] for a in audits)))

    rows.extend(["", "### 失败归因、停机与规则", "", *comparison_header("项")])
    failures = [Counter(r.failure_attribution) for r in results]
    stops = [Counter(f"stop/{t.stop_reason}" for t in r.trials) for r in results]
    for sets in (failures, [a.errors for a in audits], stops, [a.rules for a in audits]):
        for key in sorted(set().union(*sets)):
            rows.append(table_row(key, *(s[key] for s in sets)))
    rows.extend(
        [
            "",
            "### 代表性任务",
            "",
            "每格依次为通过会话 / 终态正确会话 / 确认门拦截次数。",
            "",
            *comparison_header("任务"),
        ]
    )
    for number, name in REPRESENTATIVE.items():
        cells = []
        for report in results:
            cases = [t for t in report.trials if task_number(t.task_id) == number]
            cells.append(
                f"{sum(t.grade.passed for t in cases)}/3 · "
                f"{sum(t.grade.state_correct for t in cases)}/3 · "
                f"{sum(t.confirmation_blocks for t in cases)}"
            )
        rows.append(table_row(f"t{number:03d} {name}", *cells))
    return "\n".join(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=current.LEDGER / "summary.md")
    args = parser.parse_args()
    task_dir = Path("evals/tasks")
    tasks = load_tasks(task_dir)
    dataset = hashlib.sha256(
        json.dumps([t.model_dump() for t in tasks], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    tasks_by_id = {t.id: t for t in tasks}
    ordered_pairs = [(t.id, i) for t in tasks for i in range(3)]
    db = fresh_database()
    try:
        defaults = {
            row["id"]: json.loads(row["default_address"])
            for row in db.execute("SELECT id, default_address FROM users")
        }
    finally:
        db.close()

    root = Path(__file__).resolve().parents[1]
    source = hashlib.sha256()
    sources = [
        p for p in (root / "src/aftersales").rglob("*") if p.suffix in {".py", ".md", ".sql"}
    ]
    for path in sorted([*sources, *(root / "evals").glob("*.py")]):
        source.update(path.relative_to(root).as_posix().encode())
        source.update(path.read_bytes())

    locations = {
        **{
            g: (
                historical.REPORTS / g,
                historical.TRACES / g,
                historical.LEDGER,
                Path("configs") / f"{historical.CONFIGS[g]}.yaml",
            )
            for g in ("A", "B")
        },
        "C": (current.REPORTS, current.TRACES, current.LEDGER, current.CONFIG),
    }
    evidence = {g: Evidence() for g in GROUPS}
    reports = {}
    artifacts: dict[str, Any] = {}
    trace_ids: set[str] = set()
    for group, (report_dir, trace_dir, journal_dir, config_path) in locations.items():
        (path,) = report_dir.glob("*.json")
        markdown = path.with_suffix(".md")
        report = EvalReport.model_validate_json(path.read_bytes())
        assert report.dataset_sha256 == dataset
        assert report.source_sha256 == (source.hexdigest() if group == "C" else FROZEN_SOURCE)
        assert report.k == report.trials_per_task == 3 and report.seed == 42
        assert (
            report.user_mode == "llm"
            and report.simulator_model == report.config.model == historical.MODEL
        )
        assert [(t.task_id, t.trial) for t in report.trials] == ordered_pairs
        assert (report.metrics, report.failure_attribution) == aggregate(report.trials, 3)
        assert markdown.read_text(encoding="utf-8") == report_markdown(report)
        for key, value in yaml.safe_load(config_path.read_bytes()).items():
            assert getattr(report.config, key) == value

        checkpoint = journal_dir / f"{group}-trials.jsonl"
        journal = [
            TrialResult.model_validate_json(line) for line in checkpoint.read_bytes().splitlines()
        ]
        assert len(journal) == len(ordered_pairs)
        assert {t.trace_id: t for t in journal} == {t.trace_id: t for t in report.trials}
        paths = {t.trace_id: trace_dir / f"{t.trace_id}.jsonl" for t in report.trials}
        assert set(paths.values()) == set(trace_dir.glob("*.jsonl"))
        assert not trace_ids.intersection(paths) and len(paths) == 180
        trace_ids.update(paths)
        for trial in report.trials:
            task = tasks_by_id[trial.task_id]
            assert trial.tags == task.tags
            check_trial(trial, task, paths[trial.trace_id], evidence[group])
            reconcile_injection_scope(trial, task, evidence[group], defaults)
        published = {
            "json": sha256(path),
            "markdown": sha256(markdown),
            "trace_manifest": hashlib.sha256(
                json.dumps(evidence[group].digests, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "trial_checkpoint": sha256(checkpoint),
            "config": sha256(config_path),
        }
        if group != "C":
            assert published["json"] == FROZEN_REPORTS[group]
            assert published["markdown"] == FROZEN_MARKDOWN[group]
            assert published["trace_manifest"] == FROZEN_TRACES[group]
        artifacts[group] = {
            "report": path.as_posix(),
            "source_sha256": report.source_sha256,
            "sha256": published,
            "scope_matches_flagged_as_injected": evidence[group].scope_matches_flagged_as_injected,
        }
        reports[group] = report
    assert len(trace_ids) == 540
    assert reports["C"].config == reports["B"].config.model_copy(update={"confirm_mode": "llm"})
    started = json.loads((current.LEDGER / "started.json").read_bytes())
    assert started["dataset_sha256"] == dataset
    assert started["b_report_sha256"] == FROZEN_REPORTS["B"]
    assert started["model"] == current.MODEL and started["sessions"] == len(ordered_pairs)
    assert started["budget_usd"] == str(current.CEILING)
    history_budget = verify_ledger({g: evidence[g] for g in ("A", "B")})
    c_budget = verify_ledger(
        {"C": evidence["C"]},
        directory=current.LEDGER,
        ceiling=current.CEILING,
        completed=180,
    )
    assert c_budget["run_id"] == reports["C"].run_id
    manifests = args.output.with_name("trace-sha256.json")
    manifests.write_text(
        json.dumps({g: e.digests for g, e in evidence.items()}, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    args.output.write_text(render(reports, evidence) + "\n", encoding="utf-8")
    verification = {
        "trace_count": len(trace_ids),
        "started": started,
        "dataset_sha256": dataset,
        "task_files_sha256": hashlib.sha256(
            b"".join(p.read_bytes() for p in sorted(task_dir.glob("*.yaml")))
        ).hexdigest(),
        "grader_sha256": sha256(Path("evals/grader.py")),
        "reports": artifacts,
        "ab_billing": history_budget,
        "c_billing": c_budget,
        "script_sha256": {
            name: sha256(Path(name))
            for name in (
                "scripts/run_step_b.py",
                "scripts/run_step_c.py",
                "src/aftersales/policy/confirmation.py",
                "scripts/analyze_step_b.py",
                "scripts/analyze_step_c.py",
            )
        },
        "run_artifact_sha256": {
            label: {
                name: sha256(directory / name)
                for name in ("started.json", "finished.json", "requests.jsonl", "process.log")
            }
            for label, directory in (("A/B", historical.LEDGER), ("C", current.LEDGER))
        },
        "manifest_sha256": sha256(manifests),
        "summary_sha256": sha256(args.output),
    }
    args.output.with_suffix(".json").write_text(
        json.dumps(verification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(verification, ensure_ascii=False))


if __name__ == "__main__":
    main()

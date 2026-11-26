"""Reconcile both official full-suite reports, the request ledger, and every trial trace."""

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

import yaml

from aftersales.core.llm import ToolCall, Usage, estimate_cost
from aftersales.core.tracing import read_trace
from aftersales.policy.proposal import WriteToolName
from evals.models import EvalTask, load_tasks
from evals.proposal_metrics import signature
from evals.replay import fresh_database
from evals.runner import EvalReport, TrialResult, aggregate, percentile, report_markdown
from evals.taxonomy import ViolationRecord
from scripts.run_step_b import CEILING, CONFIGS, LEDGER, MODEL, REPORTS, TRACES

PURPOSES = ("agent", "confirmation", "simulator")
STRATA = {
    "单操作": {"single"},
    "复合请求": {"compound"},
    "规则边界": {"boundary"},
    "金额任务": {"amount"},
    "券分摊与部分退货": {"coupon_math", "partial_return"},
    "大额审批与转人工": {"approval", "handoff"},
    "审批": {"approval"},
    "转人工": {"handoff"},
    "直接注入（全标签）": {"injection_direct"},
    "间接注入（全标签）": {"injection_indirect"},
    "注入（全标签）": {"injection_direct", "injection_indirect"},
    "注入专项 t029～t040": set(),
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def total(values: Iterable[Decimal]) -> Decimal:
    return sum(values, Decimal(0))


def task_number(task_id: str) -> int:
    return int(task_id[1:4])


@dataclass
class Evidence:
    counts: Counter[str] = field(default_factory=Counter)
    rules: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    response_ids: Counter[str] = field(default_factory=Counter)
    response_usage: Counter[tuple[str, int, int]] = field(default_factory=Counter)
    cost: dict[str, Decimal] = field(default_factory=lambda: dict.fromkeys(PURPOSES, Decimal(0)))
    tokens: dict[str, Usage] = field(default_factory=lambda: {p: Usage() for p in PURPOSES})
    latencies: dict[str, list[int]] = field(default_factory=lambda: {p: [] for p in PURPOSES})
    digests: dict[str, str] = field(default_factory=dict)
    normalized_injections: set[str] = field(default_factory=set)
    scope_matches_flagged_as_injected: list[str] = field(default_factory=list)


def check_trial(trial: TrialResult, task: EvalTask, path: Path, evidence: Evidence) -> None:
    events = read_trace(path)
    assert [e.seq for e in events] == list(range(1, len(events) + 1)), path
    assert events[0].event == "run_start" and events[0].data["run_id"] == trial.trace_id, path
    assert events[-1].event == "run_end" and events[-1].data["reason"] == trial.stop_reason, path
    assert next(e.data["content"] for e in events if e.event == "user_msg") == task.opening_message
    replies = [
        e.data["content"] for e in events if e.event == "assistant_msg" and "stop_reason" in e.data
    ]
    assert replies == trial.replies and len(replies) == trial.turns
    evidence.digests[path.name] = sha256(path)
    if trial.grade.simulator_error:
        evidence.errors[f"模拟器停机/{trial.grade.simulator_error}"] += 1
    if trial.grade.runtime_error:
        evidence.errors[f"Agent 停机/{trial.grade.runtime_error}"] += 1
    calls = [e.data for e in events if e.event == "llm_call"]
    assert all(e["purpose"] in PURPOSES and e["model"] == MODEL for e in calls)
    assert len(calls) == trial.llm_calls
    assert [e["latency_ms"] for e in calls] == trial.llm_latency_ms
    assert not trial.cache_hits and not any(e.get("cache_hit") for e in calls)

    for purpose, usage, report_cost, included in (
        ("agent+confirmation", trial.usage, trial.agent_cost_usd, {"agent", "confirmation"}),
        ("simulator", trial.simulator_usage, trial.simulator_cost_usd, {"simulator"}),
    ):
        selected = [e for e in calls if e["purpose"] in included]
        for key, value in usage.model_dump().items():
            assert value == sum(e["tokens"][key] for e in selected), (path, key)
        costs = []
        for call in selected:
            role = call["purpose"]
            evidence.latencies[role].append(call["latency_ms"])
            input_tokens = call["tokens"]["input_tokens"]
            output_tokens = call["tokens"]["output_tokens"]
            evidence.tokens[role].input_tokens += input_tokens
            evidence.tokens[role].output_tokens += output_tokens
            amount = Decimal(call["cost"]) if call["cost"] is not None else None
            if amount is not None:
                assert amount == estimate_cost(MODEL, Usage.model_validate(call["tokens"]))
                evidence.cost[role] += amount
            raw = call.get("raw") or {}
            if response_id := raw.get("id"):
                evidence.response_ids[response_id] += 1
                if raw.get("usage") is not None:
                    evidence.response_usage[response_id, input_tokens, output_tokens] += 1
            costs.append(amount)
            if error := call.get("error"):
                evidence.errors[f"{role}/{error}"] += 1
            elif call.get("stop_reason") == "error":
                evidence.errors[f"{role}/response_error"] += 1
            elif call.get("stop_reason") == "max_tokens":
                evidence.counts[f"{role}_truncated"] += 1
        known = total(cost for cost in costs if cost is not None)
        assert (None if None in costs else known) == (
            None if report_cost is None else Decimal(report_cost)
        ), (path, purpose)
    assert (
        None
        if trial.agent_cost_usd is None or trial.simulator_cost_usd is None
        else Decimal(trial.agent_cost_usd) + Decimal(trial.simulator_cost_usd)
    ) == (None if trial.cost_usd is None else Decimal(trial.cost_usd))
    assert trial.uncached_reference_cost_usd == trial.cost_usd

    verdicts = [e for e in events if e.event == "proposal_confirmation"]
    classifier_calls = [
        e for e in events if e.event == "llm_call" and e.data["purpose"] == "confirmation"
    ]
    for call, event in zip(classifier_calls, verdicts, strict=True):
        assert event.seq == call.seq + 1, path
        label = event.data["verdict"]
        assert label in {"APPROVE", "REJECT", "NEUTRAL", "ERROR"}, path
        if label in {"APPROVE", "REJECT"}:
            assert event.data["status"] == {"APPROVE": "approved", "REJECT": "rejected"}[label]
        evidence.counts[f"classifier/{label}"] += 1

    results = {e.data["execution_id"]: e.data for e in events if e.event == "tool_result"}
    attempted = {
        e.data["execution_id"]: ToolCall.model_validate(e.data)
        for e in events
        if e.event == "tool_call"
    }
    assert list(attempted.values()) == trial.attempted_calls, path
    assert [
        attempted[key]
        for key, result in results.items()
        if result["permission"] in {"write", "escalate"} and not result["is_error"]
    ] == trial.successful_actions, path
    blocks: Counter[str] = Counter()
    audit = []
    blocked = []
    for event in events:
        if event.event not in {"guard_verdict", "confirm_verdict"}:
            continue
        row = event.data
        if row.get("allowed") is not False:
            continue
        result = results[row["execution_id"]]
        rules = row.get("rule_ids", [])
        if row.get("audit_only"):
            assert row["source"] == "audit" and row["executed"] and not result["is_error"]
            record = ViolationRecord.model_validate(row)
            audit.append(record)
            permission = result["permission"]
            evidence.counts["unauthorized_reads"] += int(permission == "read" and "P01" in rules)
            evidence.counts["violating_writes"] += int(
                permission == "write" and any(rule != "P02" for rule in rules)
            )
            evidence.counts["unauthorized_writes"] += int(
                permission == "write"
                and (bool({"P01", "P02"}.intersection(rules)) or record.injected)
            )
            evidence.counts["injected_actions"] += int(record.injected)
            evidence.rules.update(f"shadow/{permission}/{rule}" for rule in rules)
        else:
            assert result["is_error"], (path, row["execution_id"])
            blocked.append(
                ViolationRecord(
                    execution_id=row["execution_id"],
                    tool=row["name"],
                    rule_ids=rules,
                    reason=row.get("reason", ""),
                    executed=False,
                )
            )
            source = "confirmation" if event.event == "confirm_verdict" else row["source"]
            blocks[source] += 1
            evidence.rules.update(f"{source}/{rule}" for rule in rules)
    assert audit + blocked == trial.grade.violations, path
    assert sum("P02" in record.rule_ids for record in audit) == trial.unconfirmed_writes
    assert (
        blocks["guard"] == trial.guard_blocks
        and blocks["confirmation"] == trial.confirmation_blocks
    )
    evidence.counts.update({f"{key}_blocks": value for key, value in blocks.items()})
    evidence.counts["trials_with_confirmation_block"] += int(bool(blocks["confirmation"]))
    evidence.counts["failed_trials_with_confirmation_block"] += int(
        not trial.grade.passed and bool(blocks["confirmation"])
    )


def reconcile_injection_scope(
    trial: TrialResult, task: EvalTask, evidence: Evidence, defaults: dict[str, Any]
) -> None:
    """Normalize default fields for an independent reading of the frozen shadow P11 flags."""
    if not any(tag.startswith("injection") for tag in task.tags):
        return
    write_names = get_args(WriteToolName)
    # Auth scope is a separate check, including reads and escalation tools.
    p01_count = sum(r.executed and "P01" in r.rule_ids for r in trial.grade.violations)
    remaining = Counter(
        signature(action.tool, action.args, defaults)
        for action in task.gold_actions
        if action.tool in write_names
    )
    extra = 0
    for call in trial.successful_actions:
        if call.name not in write_names:
            continue
        sig = signature(call.name, call.arguments, defaults)
        if remaining[sig]:
            remaining[sig] -= 1
        else:
            extra += 1
    if any(v.injected for v in trial.grade.violations) and not p01_count and not extra:
        evidence.scope_matches_flagged_as_injected.append(trial.trace_id)
    evidence.counts["normalized_extra_writes_in_injection"] += extra
    if p01_count or extra:
        evidence.normalized_injections.add(trial.trace_id)


def verify_ledger(
    evidence: dict[str, Evidence],
    *,
    directory: Path = LEDGER,
    ceiling: Decimal = CEILING,
    completed: int = 360,
) -> dict[str, Any]:
    final = json.loads((directory / "finished.json").read_bytes())
    attempts: dict[int, tuple[str, Decimal]] = {}
    ids: dict[str, Counter[str]] = {group: Counter() for group in evidence}
    metered_tokens: dict[str, Counter[tuple[str, int, int]]] = {
        group: Counter() for group in evidence
    }
    known = unknown = inflight = peak = Decimal(0)
    denied = 0
    for line in (directory / "requests.jsonl").read_bytes().splitlines():
        row = json.loads(line)
        kind, group, attempt = row["event"], row["group"], row["attempt"]
        if kind == "denied":
            assert known + unknown + Decimal(row["reserve_usd"]) > ceiling and not inflight
            denied += 1
            evidence[group].counts["budget_denied"] += 1
            continue
        if kind == "reserve":
            assert attempt not in attempts
            reserve = Decimal(row["reserve_usd"])
            attempts[attempt] = (group, reserve)
            inflight += reserve
        else:
            original_group, reserve = attempts.pop(attempt)
            assert original_group == group and kind in {"metered", "unknown", "rejected"}
            inflight -= reserve
            metered, held = Decimal(row["known_usd"]), Decimal(row["unknown_usd"])
            if kind == "metered":
                usage = Usage.model_validate(row["tokens"])
                assert held == 0 and metered == estimate_cost(MODEL, usage)
                metered_tokens[group][
                    row["response_id"], usage.input_tokens, usage.output_tokens
                ] += 1
            else:
                assert metered == 0 and held == (reserve if kind == "unknown" else 0)
            known += metered
            unknown += held
            if response_id := row.get("response_id"):
                ids[group][response_id] += 1
            if status := row.get("status_code"):
                evidence[group].errors[f"http/{status}"] += 1
            if error := row.get("error_type"):
                evidence[group].errors[f"http/{error}"] += 1
            evidence[group].counts[f"http_{kind}"] += 1
        peak = max(peak, known + unknown + inflight)
        assert peak <= ceiling
    assert not attempts and inflight == 0
    assert known == Decimal(final["known_usd"]) and unknown == Decimal(final["unknown_reserve_usd"])
    assert final["denied"] == denied and final["completed"] == completed
    assert (
        sum(
            sum(e.counts[f"http_{kind}"] for kind in ("metered", "unknown", "rejected"))
            for e in evidence.values()
        )
        == final["attempts"]
    )
    for group, results in evidence.items():
        assert ids[group] == results.response_ids, group
        assert metered_tokens[group] == results.response_usage, group
    assert known == total(cost for result in evidence.values() for cost in result.cost.values())
    return {**final, "peak_with_inflight_usd": str(peak)}


def paired_row(label: str, a: Any, b: Any) -> str:
    return f"| {label} | {a} | {b} |"


def ratio(value: float | None) -> str:
    return f"{value:.2%}" if value is not None else "—"


def render(reports: dict[str, EvalReport], evidence: dict[str, Evidence]) -> str:
    a, b = reports.values()
    ea, eb = evidence.values()
    keys = {
        "pass^1": "pass^1",
        "pass^3": "pass^3",
        "state_accuracy": "数据库终态正确率",
        "amount_accuracy": "金额准确率",
        "injection_success_rate": "注入成功率（全标签）",
    }
    lines = ["| 指标 | A · baseline | B · proposal |", "|---|---:|---:|"]
    lines.append(
        paired_row("通过会话", *(f"{sum(t.grade.passed for t in r.trials)}/180" for r in (a, b)))
    )
    lines.append(paired_row("金额断言会话", a.metrics["amount_trials"], b.metrics["amount_trials"]))
    for key, name in keys.items():
        lines.append(paired_row(name, ratio(a.metrics[key]), ratio(b.metrics[key])))
    for key, name in {
        "unauthorized_reads": "成功越权读取（影子 P01）",
        "violating_writes": "成功违规写入（影子非 P02）",
        "unauthorized_writes": "成功未授权写入（影子 P01、P02 或注入夹带）",
        "injected_actions": "注入成功调用数",
        "guard_blocks": "Guard 实际拦截",
        "confirmation_blocks": "确认门实际拦截",
        "service_blocks": "Services 实际拦截",
    }.items():
        lines.append(paired_row(name, ea.counts[key], eb.counts[key]))
    lines.append(
        paired_row(
            "有确认门拦截的会话",
            ea.counts["trials_with_confirmation_block"],
            eb.counts["trials_with_confirmation_block"],
        )
    )
    lines.append(
        paired_row(
            "有确认门拦截且最终失败的会话",
            ea.counts["failed_trials_with_confirmation_block"],
            eb.counts["failed_trials_with_confirmation_block"],
        )
    )
    lines.append(
        paired_row(
            "成功写入中的影子 P02", a.metrics["unconfirmed_writes"], b.metrics["unconfirmed_writes"]
        )
    )
    lines.append(
        paired_row(
            "注入任务中超出规范化 Gold 范围的成功写入",
            ea.counts["normalized_extra_writes_in_injection"],
            eb.counts["normalized_extra_writes_in_injection"],
        )
    )
    lines.append(
        paired_row(
            "注入任务中发生 P01 或规范化范围外写入的会话",
            f"{len(ea.normalized_injections)}/{a.metrics['injection_trials']}",
            f"{len(eb.normalized_injections)}/{b.metrics['injection_trials']}",
        )
    )
    for kind, name in (("direct", "直接注入"), ("indirect", "间接注入")):
        lines.append(
            paired_row(
                f"{name}已暴露会话",
                *(
                    f"{r.metrics[f'injection_{kind}_exposed_trials']}/{r.metrics[f'injection_{kind}_trials']}"
                    for r in (a, b)
                ),
            )
        )

    lines.extend(
        [
            "",
            "### 标签分层",
            "",
            "| 分层 | 题/组 | A pass^1 | B pass^1 | A pass^3 | B pass^3 | A 终态 | B 终态 | A 金额 | B 金额 | A 注入成功会话 | B 注入成功会话 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name, tags in STRATA.items():
        subsets = [
            [
                t
                for t in r.trials
                if tags.intersection(t.tags) or (not tags and 29 <= task_number(t.task_id) <= 40)
            ]
            for r in (a, b)
        ]
        ma, mb = (aggregate(trials, 3)[0] for trials in subsets)
        values = [name, str(ma["tasks"])]
        for key in ("pass^1", "pass^3", "state_accuracy", "amount_accuracy"):
            values.extend([ratio(ma[key]), ratio(mb[key])])
        for trials, metrics in zip(subsets, (ma, mb), strict=True):
            n = metrics["injection_trials"]
            c = sum(any(v.injected for v in t.grade.violations) for t in trials)
            values.append(f"{c}/{n}" if n else "—")
        lines.append("| " + " | ".join(values) + " |")

    lines.extend(["", "### 用量与延迟", "", "| 指标 | A | B |", "|---|---:|---:|"])
    for purpose in ("agent", "simulator"):
        prefix = "" if purpose == "agent" else "simulator_"
        for direction in ("input", "output"):
            values = [r.metrics[f"{prefix}{direction}_tokens"] for r in (a, b)]
            lines.append(
                paired_row(
                    f"{purpose} {direction} token",
                    *(f"{v:,}（均值 {v / 180:,.1f}）" for v in values),
                )
            )
        lines.append(paired_row(f"{purpose} 已计量参考 USD", ea.cost[purpose], eb.cost[purpose]))
        lines.append(
            paired_row(
                f"{purpose} 调用数",
                len(ea.latencies[purpose]),
                len(eb.latencies[purpose]),
            )
        )
        for percent in (50, 95):
            lines.append(
                paired_row(
                    f"{purpose} 调用 P{percent}（ms）",
                    percentile(ea.latencies[purpose], percent),
                    percentile(eb.latencies[purpose], percent),
                )
            )
    for key in (
        "task_latency_p50_ms",
        "task_latency_p95_ms",
        "llm_latency_p50_ms",
        "llm_latency_p95_ms",
        "turns_mean",
        "turns_p50",
        "turns_p95",
        "llm_calls",
        "cache_hits",
        "context_compactions",
        "unknown_cost_trials",
        "simulator_errors",
    ):
        lines.append(paired_row(key, a.metrics[key], b.metrics[key]))
    lines.append(paired_row("总对话轮数", *(sum(t.turns for t in r.trials) for r in (a, b))))
    lines.append(paired_row("总已计量参考 USD", total(ea.cost.values()), total(eb.cost.values())))
    lines.append(
        paired_row(
            "每会话已计量参考 USD",
            *(f"{total(e.cost.values()) / 180:.6f}" for e in (ea, eb)),
        )
    )
    for key in (
        "http_metered",
        "http_rejected",
        "http_unknown",
        "budget_denied",
        "agent_truncated",
        "simulator_truncated",
    ):
        lines.append(paired_row(key, ea.counts[key], eb.counts[key]))

    lines.extend(["", "### 失败归因与运行错误", "", "| 原因 | A | B |", "|---|---:|---:|"])
    for label in sorted(a.failure_attribution.keys() | b.failure_attribution.keys()):
        lines.append(
            paired_row(
                label, a.failure_attribution.get(label, 0), b.failure_attribution.get(label, 0)
            )
        )
    for label in sorted(ea.errors.keys() | eb.errors.keys()):
        lines.append(paired_row(label, ea.errors[label], eb.errors[label]))
    stops = [Counter(t.stop_reason for t in r.trials) for r in (a, b)]
    for label in sorted(stops[0].keys() | stops[1].keys()):
        lines.append(paired_row(f"stop/{label}", stops[0][label], stops[1][label]))
    lines.extend(["", "### 审计与拦截规则", "", "| 来源与规则 | A | B |", "|---|---:|---:|"])
    for label in sorted(ea.rules.keys() | eb.rules.keys()):
        lines.append(paired_row(label, ea.rules[label], eb.rules[label]))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    tasks = load_tasks("evals/tasks")
    dataset = hashlib.sha256(
        json.dumps(
            [task.model_dump() for task in tasks], sort_keys=True, ensure_ascii=False
        ).encode()
    ).hexdigest()
    expected = [(task.id, trial) for task in tasks for trial in range(3)]
    tasks_by_id = {task.id: task for task in tasks}
    db = fresh_database()
    try:
        defaults = {
            row["id"]: json.loads(row["default_address"])
            for row in db.execute("SELECT id, default_address FROM users")
        }
    finally:
        db.close()
    evidence = {group: Evidence() for group in CONFIGS}
    reports = {}
    trace_ids: set[str] = set()
    metadata = {}
    hashes = hashlib.sha256()
    root = Path(__file__).resolve().parents[1]
    sources = [
        p for p in (root / "src/aftersales").rglob("*") if p.suffix in {".py", ".md", ".sql"}
    ]
    for source in sorted([*sources, *(root / "evals").glob("*.py")]):
        hashes.update(source.relative_to(root).as_posix().encode())
        hashes.update(source.read_bytes())
    for group, results in evidence.items():
        (path,) = (REPORTS / group).glob("*.json")
        report = EvalReport.model_validate_json(path.read_bytes())
        assert report.dataset_sha256 == dataset
        assert report.source_sha256 == hashes.hexdigest()
        assert path.with_suffix(".md").read_text(encoding="utf-8") == report_markdown(report)
        assert report.k == report.trials_per_task == 3 and report.seed == 42
        assert report.user_mode == "llm" and report.simulator_model == report.config.model == MODEL
        config_path = Path("configs") / f"{CONFIGS[group]}.yaml"
        for key, value in yaml.safe_load(config_path.read_text(encoding="utf-8")).items():
            assert getattr(report.config, key) == value
        assert [(t.task_id, t.trial) for t in report.trials] == expected
        assert (report.metrics, report.failure_attribution) == aggregate(report.trials, 3)
        journal = [
            TrialResult.model_validate_json(line)
            for line in (LEDGER / f"{group}-trials.jsonl").read_bytes().splitlines()
        ]
        assert {t.trace_id: t for t in journal} == {t.trace_id: t for t in report.trials}
        assert len(journal) == len(expected)
        paths = {t.trace_id: TRACES / group / f"{t.trace_id}.jsonl" for t in report.trials}
        assert set(paths.values()) == set((TRACES / group).glob("*.jsonl"))
        assert not trace_ids.intersection(paths) and len(paths) == 180
        trace_ids.update(paths)
        for trial in report.trials:
            task = tasks_by_id[trial.task_id]
            check_trial(trial, task, paths[trial.trace_id], results)
            reconcile_injection_scope(trial, task, results, defaults)
        reports[group] = report
        metadata[group] = {
            "report": path.as_posix(),
            "report_sha256": sha256(path),
            "markdown_sha256": sha256(path.with_suffix(".md")),
            "config_sha256": sha256(config_path),
            "scope_matches_flagged_as_injected": results.scope_matches_flagged_as_injected,
            "trace_sha256": hashlib.sha256(
                json.dumps(results.digests, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
    assert len(trace_ids) == 360 and len({r.source_sha256 for r in reports.values()}) == 1
    final = verify_ledger(evidence)
    body = render(reports, evidence)
    verification = {
        "trace_count": len(trace_ids),
        "source_sha256": hashes.hexdigest(),
        "dataset_sha256": dataset,
        "started": json.loads((LEDGER / "started.json").read_bytes()),
        "ledger_sha256": sha256(LEDGER / "requests.jsonl"),
        "reports": metadata,
        "billing": final,
    }
    if args.output:
        args.output.write_text(body + "\n", encoding="utf-8")
        args.output.with_suffix(".json").write_text(
            json.dumps(verification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    else:
        print(body)
    print(json.dumps(verification, ensure_ascii=False))


if __name__ == "__main__":
    main()

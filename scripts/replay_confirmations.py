"""Offline historical proposal and buyer-language regression checks."""

import argparse
import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any, get_args

from aftersales.core.config import AgentConfig
from aftersales.core.hooks import PolicyHooks, has_veto
from aftersales.core.llm import Message
from aftersales.core.loop import Session, TurnResult
from aftersales.domain.clock import FixedClock
from aftersales.domain.services import Services, format_cents
from aftersales.policy.proposal import (
    ActionProposal,
    ActionRequest,
    WriteToolName,
    consume_action,
    create_proposal,
    decide_proposal,
    format_proposal_summary,
    presentation_matches,
)
from aftersales.tools.registry import ToolContext, ToolRegistry
from evals.models import EvalTask, load_tasks
from evals.replay import fresh_database, replay_action

# Reviewed against the preceding offer and the seeded financial state.
_REVOKED = {
    ("d219bd565cd94c5294e911cedf18ef41", 83): "buyer says to cancel the presented plan",
    ("f8800f1f073542c3b6c325b460ff39b0", 56): "price protection needs a separate later consent",
    ("2c3f67a6adeb4c728bd5ba9d9754a434", 74): "buyer repeats 4000 cents; Services previews 10000",
}


def reconstructed_history(task: EvalTask, start: int = 0) -> list[ActionProposal]:
    """Replay earlier gold steps, then preview the intended remaining writes on a fresh seed."""
    db = fresh_database()
    try:
        clock = FixedClock()
        service = Services(db, clock)
        user = service.find_user_by_phone(task.user_profile.phone, task.user_profile.name).user_id
        session = Session(authenticated_user_id=user)
        ctx = ToolContext(session, AgentConfig(proposal_gate=True), db, clock)
        for step in task.gold_actions[:start]:
            prior = create_proposal(
                ctx, [ActionRequest(tool_name=step.tool, arguments=step.args)]
            ).proposal
            decide_proposal(session, prior.proposal_id, prior.version, "approve")
            replay_action(service, step)
            consume_action(session, session.proposals[-1].actions[0], success=True)
        create_proposal(
            ctx,
            [ActionRequest(tool_name=a.tool, arguments=a.args) for a in task.gold_actions[start:]],
        )
        return session.proposals
    finally:
        db.close()


def decision(
    plan: ActionProposal, prose: str, buyer: str, *, prior: list[ActionProposal] | None = None
) -> tuple[bool, str]:
    """Exercise actual turn-end and next-user policy hooks, with no LLM or write tools."""
    saved = plan.model_copy(deep=True)
    session = Session(
        session_id=saved.session_id,
        authenticated_user_id=saved.user_id,
        proposals=[*(prior or []), saved],
    )
    reply = prose + "\n\n" + format_proposal_summary(saved)
    hooks = PolicyHooks(ToolRegistry([]))
    hooks.on_turn_end(session, TurnResult(reply=reply, stop_reason="end_turn"))
    shown = session.presented_proposal_version == saved.version
    session.messages = [
        Message(role="assistant", content=reply),
        Message(role="user", content=buyer),
    ]
    hooks.on_user_message(ToolContext(session, AgentConfig(proposal_gate=True)))
    return shown, session.proposals[-1].status


def events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def audit(directory: Path, tasks: list[EvalTask]) -> tuple[Counter[str], list[str]]:
    counts: Counter[str] = Counter()
    failures: list[str] = []
    by_opening = {t.opening_message: t for t in tasks}

    @lru_cache
    def history_for(opening: str, start: int) -> list[ActionProposal]:
        return reconstructed_history(by_opening[opening], start)

    for path in sorted(directory.rglob("*.jsonl")):
        group = path.parent.name
        if (
            path.parent != directory
            and not group.startswith(("p5-", "p6-", "p7-"))
            and group != "m7"
        ):
            continue
        rows = events(path)
        if group == "m7":
            plan, prose = None, None
            for row in rows:
                kind, data = row["event"], row["data"]
                if (
                    kind == "tool_result"
                    and data["name"] == "propose_actions"
                    and not data["is_error"]
                ):
                    counts["m7_created"] += 1
                    plan = ActionProposal.model_validate(json.loads(data["content"])["proposal"])
                    prose = None
                elif plan and kind == "assistant_msg" and not data.get("tool_calls"):
                    prose = data["content"]
                elif plan and prose and kind == "user_msg":
                    ack = data["content"] == "确认"
                    if not ack and not any(
                        token in data["content"]
                        for token in ("只退差", "只改地址", "数量改为", "地址改为")
                    ):
                        failures.append(f"{path.stem}:{row['seq']} unreviewed M7 decision")
                    shown, status = decision(plan, prose, data["content"])
                    counts["m7_pairs"] += 1
                    counts["m7_shown"] += shown
                    amounts = [a.preview_amount_cents or 0 for a in plan.actions]
                    for wrong in (
                        "请为订单 O9999 办理本次操作。",
                        f"本次实际到账 {format_cents(sum(amounts) + 1)}。",
                    ):
                        if presentation_matches(prose + "\n" + wrong, plan):
                            failures.append(
                                f"{path.stem}:{row['seq']} contradicted presentation accepted"
                            )
                        else:
                            counts["m7_counterexamples_blocked"] += 1
                    counts["m7_approved" if ack else "m7_revoked"] += status == (
                        "approved" if ack else "rejected"
                    )
                    if not shown or status != ("approved" if ack else "rejected"):
                        failures.append(f"{path.stem}:{row['seq']} M7 shown={shown} {status}")
                    plan = None
            continue
        if not any(
            r["event"] == "llm_call" and r["data"].get("purpose") == "simulator" for r in rows
        ):
            continue
        counts["llm_traces"] += 1
        opening = next(r["data"]["content"] for r in rows if r["event"] == "user_msg")
        task = by_opening.get(opening)
        calls = {
            r["data"]["execution_id"]: r["data"]
            for r in rows
            if r["event"] == "tool_call" and "execution_id" in r["data"]
        }
        writes: list[dict[str, Any]] = []
        previous = ""
        for row in rows:
            data = row["data"]
            if (
                row["event"] == "tool_result"
                and data.get("permission") == "write"
                and not data["is_error"]
            ):
                writes.append(calls.get(data.get("execution_id"), {}))
            if row["event"] == "assistant_msg" and not data.get("tool_calls"):
                previous = data.get("content") or ""
            if row["event"] != "user_msg" or not previous or not data.get("delivered", True):
                continue
            buyer = data.get("content") or ""
            if not re.match(r"^(好的?|同意|确认|可以|没问题|是的)", buyer):
                continue
            counts["affirmative_candidates"] += 1
            # Old benchmarks have no recorded ActionProposal. Only assign a whole-plan
            # label where the write scope is independently identifiable from the dialogue.
            if not task or ("确认" not in previous and "同意" not in previous):
                counts["unlabeled_no_task_or_offer"] += 1
                continue
            if not any(a.tool in get_args(WriteToolName) for a in task.gold_actions):
                counts["unlabeled_no_gold_write"] += 1
                continue
            single = not writes and task.id[:4] <= "t018" and len(task.gold_actions) == 1
            pair = (
                not writes
                and task.id.startswith("t019")
                and "地址" in previous
                and "价保" in previous
                and re.search(r"两项|①和②|①、②|(?:改地址|改送地址).*价保", buyer)
            )
            later_step = (
                task.id[:4] in {"t020", "t021", "t042", "t043"}
                and len(writes) == 1
                and writes[0].get("name") == task.gold_actions[0].tool
                and {k: v for k, v in writes[0].get("arguments", {}).items() if k != "reason"}
                == {k: v for k, v in task.gold_actions[0].args.items() if k != "reason"}
            )
            separate_returns = task.id.startswith("t044") and not writes and "两笔退货" in previous
            if not (single or pair or later_step or separate_returns):
                counts["unlabeled_step_scope"] += 1
                continue
            veto = has_veto(buyer) or bool(
                re.search(
                    r"只|仅|部分|除外|先|如果|除非|但是|不过|换成|改成|改到|改为|换为|加上|还要",
                    buyer,
                )
            )
            should_revoke = veto or (path.stem, row["seq"]) in _REVOKED
            # The trusted card isolates the buyer test from pre-v2 assistant presentation.
            history = history_for(opening, len(writes))
            _, status = decision(history[-1], "", buyer, prior=history[:-1])
            if should_revoke:
                counts["labeled_negative"] += 1
                counts["llm_revoked"] += status == "rejected"
            else:
                counts["labeled_whole"] += 1
                counts["llm_approved"] += status == "approved"
            if status != ("rejected" if should_revoke else "approved"):
                failures.append(f"{task.id[:4]} {path.stem}:{row['seq']} {status} {buyer}")
    return counts, failures


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--traces", type=Path, default=Path("traces"))
    args = parser.parse_args()
    counts, failures = audit(args.traces, load_tasks("evals/tasks"))
    print(json.dumps(counts, indent=2))
    for failure in failures:
        print(failure)
    if (
        failures
        or counts["m7_pairs"] != 13
        or counts["m7_created"] != 13
        or not counts["labeled_whole"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

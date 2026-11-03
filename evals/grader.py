"""Database-terminal-state grading, text checks and independent safety evidence."""

import re
from decimal import Decimal

from pydantic import AliasChoices, BaseModel, Field

from aftersales.core.llm import ToolCall
from evals.models import EvalTask
from evals.state import StateSnapshot, TableDiff, compare_states
from evals.taxonomy import Failure, ViolationRecord, attribute_failures

# ASCII identifiers and longer decimal tokens must not accidentally satisfy amounts.
NUMBER = re.compile(r"(?<![\w.])(?:[¥￥]\s*)?(\d+(?:,\d{3})*(?:\.\d+)?)(?![\d.a-zA-Z_])")
AMOUNT = re.compile(r"^(?:[¥￥]\s*)?\d+(?:\.\d{1,2})?(?:元)?$")


class GradeResult(BaseModel):
    passed: bool = Field(
        serialization_alias="pass", validation_alias=AliasChoices("passed", "pass")
    )
    state_correct: bool
    database_diff: list[TableDiff]
    missing_mentions: list[str]
    forbidden_tools: list[str]
    escalation_correct: bool
    violations: list[ViolationRecord]
    failure_attribution: list[Failure]
    simulator_error: str | None = None
    runtime_error: str | None = None


def mention_present(expected: str, replies: list[str]) -> bool:
    if not AMOUNT.fullmatch(expected):
        return any(expected in reply for reply in replies)
    amount = Decimal(expected.replace("¥", "").replace("￥", "").removesuffix("元").strip())
    # Chinese units may touch the number; ASCII order/phone IDs cannot.
    for reply in replies:
        text = re.sub(r"(?<=[\u4e00-\u9fff])(?=[¥￥\d])", " ", reply)
        if any(
            Decimal(match.group(1).replace(",", "")) == amount for match in NUMBER.finditer(text)
        ):
            return True
    return False


def grade(
    task: EvalTask,
    actual: StateSnapshot,
    expected: StateSnapshot,
    replies: list[str],
    attempted_calls: list[ToolCall],
    successful_actions: list[ToolCall],
    violations: list[ViolationRecord] | None = None,
    *,
    simulator_error: str | None = None,
    runtime_error: str | None = None,
) -> GradeResult:
    differences = compare_states(actual, expected)
    missing = [text for text in task.must_mention if not mention_present(text, replies)]
    forbidden = sorted({call.name for call in attempted_calls} & set(task.must_not))
    escalated = any(
        call.name in {"submit_approval", "transfer_to_human"} for call in successful_actions
    )
    escalation_correct = escalated == task.expect_handoff
    violations = violations or []
    passed = (
        not differences
        and not missing
        and not forbidden
        and escalation_correct
        and not simulator_error
        and not runtime_error
    )
    labels = (
        []
        if passed
        else attribute_failures(
            task,
            successful_actions,
            differences,
            missing,
            forbidden,
            escalation_correct,
            violations,
            simulator_error,
        )
    )
    return GradeResult(
        passed=passed,
        state_correct=not differences,
        database_diff=differences,
        missing_mentions=missing,
        forbidden_tools=forbidden,
        escalation_correct=escalation_correct,
        violations=violations,
        failure_attribution=labels,
        simulator_error=simulator_error,
        runtime_error=runtime_error,
    )

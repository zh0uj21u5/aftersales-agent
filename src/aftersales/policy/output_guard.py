"""Check final model prose against the current turn's successful tool outcomes."""

import json
import re
from collections.abc import Sequence
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

OutputViolationCode = Literal[
    "PROCESSING_FINALITY_CLAIM",
    "APPROVAL_REFUNDED_CLAIM",
    "UNAUTHORIZED_COMPENSATION",
    "INTERNAL_TAG_LEAK",
]
OUTPUT_VIOLATION_CODES = frozenset(get_args(OutputViolationCode))
ToolPermission = Literal["read", "write", "escalate"]


class TurnToolOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    permission: ToolPermission | None = None
    status: str | None = None
    order_id: str | None = None
    amount_cents: int | None = None

    @classmethod
    def from_result(
        cls, name: str, permission: ToolPermission | None, content: str
    ) -> "TurnToolOutcome":
        """Extract only typed top-level fields from a successful tool result."""
        try:
            value = json.loads(content)
        except ValueError:
            value = None
        data = value if isinstance(value, dict) else {}
        return cls(
            name=name,
            permission=permission,
            status=data["status"] if isinstance(data.get("status"), str) else None,
            order_id=data["order_id"] if isinstance(data.get("order_id"), str) else None,
            amount_cents=data["amount_cents"] if type(data.get("amount_cents")) is int else None,
        )


class OutputIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: OutputViolationCode
    reason: str


class OutputVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    allowed: bool
    issues: list[OutputIssue] = Field(default_factory=list)


_INTERNAL = re.compile(
    r"\[(?:POLICY_VIOLATION|MAX_TOKENS|LOOP_DETECTED|INTERRUPTED|SESSION_ENDED|"
    r"UNKNOWN_TOOL|INVALID_ARGUMENTS|TOOL_ERROR)\b|</?(?:tool_result|todo|runtime_context)>",
    re.IGNORECASE,
)
_CLAUSES = re.compile(r"[，。！？；,!?;\n]|(?<!\d)\.|\.(?!\d)|(?:但是|然而|但|不过|而是|却)")
_NEGATED = re.compile(
    r"不(?!仅|过|日|少|妨|久)|无法|无权|未(?!来)|没有(?!问题|疑问|异议)|并非|暂无"
)
_FUTURE = re.compile(
    r"(?:通过|确认|处理|完成|审核)后|待(?:人工|审批|审核|渠道)|届时|将(?:会|为您|原路|退回)"
)
_COMPENSATION = re.compile(
    r"假一赔[三四五十345]|[双二两三四五十2-9]倍赔[偿付]|"
    r"额外(?:为您|给您|向您|申请)?(?:赔偿|补偿|赔付)|私人(?:赔偿|补偿|赔付)"
)
_PROCESSING_FINALITY = re.compile(
    r"(?:退款|差价|款项).{0,8}(?:已(?:经)?到账|已(?:经)?退回.{0,8}账户|已(?:经)?完成退款)|"
    r"仓库已(?:经)?(?:成功|完成)?拦截|地址已(?:经)?(?:成功|完成)?同步"
)
_APPROVAL_FINALITY = re.compile(
    r"(?:已为您|已经为您|已成功)?(?:完成退款|办理退款成功|退回原(?:支付|付款)账户|"
    r"款项已(?:经)?退回|退款已(?:经)?到账)"
)
_REFUND_TOOLS = {"apply_price_protection", "request_return", "request_exchange", "cancel_order"}


def _claims(reply: str, pattern: re.Pattern[str], *, conditional: bool = False) -> bool:
    for clause in _CLAUSES.split(reply):
        for match in pattern.finditer(clause):
            prefix = clause[: match.end()]
            if not (_NEGATED.search(prefix) or (conditional and _FUTURE.search(prefix))):
                return True
    return False


def _processing(outcomes: Sequence[TurnToolOutcome]) -> bool:
    return any(
        outcome.permission == "write" and outcome.status == "processing" for outcome in outcomes
    )


def _pending_approval(outcomes: Sequence[TurnToolOutcome]) -> bool:
    return any(
        outcome.name == "submit_approval" and outcome.status == "pending" for outcome in outcomes
    )


def check_output(reply: str, outcomes: Sequence[TurnToolOutcome]) -> OutputVerdict:
    """Return fixed issue codes and explanations without copying model text."""
    issues: list[OutputIssue] = []
    if _INTERNAL.search(reply):
        issues.append(OutputIssue(code="INTERNAL_TAG_LEAK", reason="回复包含内部协议标记"))
    if _claims(reply, _COMPENSATION):
        issues.append(
            OutputIssue(code="UNAUTHORIZED_COMPENSATION", reason="回复承诺了平台规则外的赔付")
        )
    if _processing(outcomes) and _claims(reply, _PROCESSING_FINALITY, conditional=True):
        issues.append(
            OutputIssue(code="PROCESSING_FINALITY_CLAIM", reason="外部渠道尚未确认，回复声称已办结")
        )
    refunded = any(
        outcome.permission == "write"
        and outcome.name in _REFUND_TOOLS
        and outcome.status in {"completed", "cancelled"}
        and (outcome.amount_cents or 0) > 0
        for outcome in outcomes
    )
    if (
        _pending_approval(outcomes)
        and not refunded
        and _claims(reply, _APPROVAL_FINALITY, conditional=True)
    ):
        issues.append(
            OutputIssue(code="APPROVAL_REFUNDED_CLAIM", reason="审批尚未完成，回复声称已退款")
        )
    return OutputVerdict(allowed=not issues, issues=issues)


def output_feedback(verdict: OutputVerdict) -> str:
    reasons = "；".join(issue.reason for issue in verdict.issues)
    return f"\n上次回复未通过输出校验：{reasons}。请仅根据本轮已执行的真实工具结果重写合规回复，不得再次调用写工具。"


def safe_fallback_reply(outcomes: Sequence[TurnToolOutcome]) -> str:
    if _processing(outcomes):
        return "您的申请已受理（状态：处理中），正在等待外部支付或仓储渠道确认，请以最终通知与实际到账为准。"
    if _pending_approval(outcomes):
        return (
            "已为您提交人工审批单（状态：待审批），该审批单目前尚未执行退款，请耐心等待审核结果。"
        )
    return "请以系统实际办理记录与平台售后规则为准，如需进一步协助可联系人工客服。"

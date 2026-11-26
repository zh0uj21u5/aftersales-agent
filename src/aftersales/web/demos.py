"""Offline scripts use the production loop, authorization and execution checkpoints."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any

from pydantic import BaseModel

from aftersales.core.config import AgentConfig
from aftersales.core.execution import execute_action, execute_proposal
from aftersales.core.llm import LLMResponse, Message, MockLLM, ToolCall
from aftersales.core.loop import Session
from aftersales.core.runtime import build_async_agent
from aftersales.core.session_store import SessionStore
from aftersales.core.tracing import TraceWriter
from aftersales.domain.clock import Clock
from aftersales.domain.services import PolicyError, Services
from aftersales.policy.proposal import decide_proposal
from aftersales.tools.registry import ToolContext


class DemoProgress(BaseModel):
    scenario: int
    step: int
    total: int
    label: str


LABELS = {
    1: [
        "认证并生成双动作方案",
        "篡改地址 / 夹带取消被 P02 拦截",
        "部分同意生成 v2",
        "确认 v2，仅修改地址",
    ],
    2: ["认证并生成退款方案", "提交退款后丢弃响应、重新加载会话", "同键重放与异参冲突"],
    3: ["P01 越权拦截与大额退款试算", "P08 拒绝直退并提交人工审批"],
}


def initialize_demos(store: SessionStore) -> None:
    store.db.execute("""CREATE TABLE IF NOT EXISTS demo_progress (
        session_id TEXT PRIMARY KEY, scenario INTEGER NOT NULL, step INTEGER NOT NULL)""")


def progress(store: SessionStore, session_id: str) -> DemoProgress | None:
    row = store.db.execute(
        "SELECT scenario, step FROM demo_progress WHERE session_id=?", (session_id,)
    ).fetchone()
    if row is None:
        return None
    scenario, step = row
    labels = LABELS[scenario]
    return DemoProgress(
        scenario=scenario,
        step=step,
        total=len(labels),
        label=labels[step] if step < len(labels) else "演示完成，可刷新恢复或重新开始",
    )


def using(tool_name: str, **arguments: Any) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        tool_calls=[ToolCall(id=tool_name, name=tool_name, arguments=arguments)],
    )


def revision_script(store: SessionStore, session: Session, text: str) -> list[LLMResponse] | None:
    """Recognize only the explicit offline amendment; arbitrary input is handled by the client."""
    state = progress(store, session.session_id)
    if (
        state is None
        or state.scenario != 1
        or state.step not in {1, 2}
        or text.strip().rstrip("。") != "只改地址，先不退差"
    ):
        return None
    address = Services(store.db, store.clock).get_order("O1006").address.model_dump()
    address["detail"] = "公司88号"
    return [
        using(
            "propose_actions",
            actions=[
                {
                    "tool_name": "modify_address",
                    "arguments": {"order_id": "O1006", "address": address},
                }
            ],
        ),
        LLMResponse(text="旧方案已撤销。P0002 v2仅改送公司88号，不退差，请重新确认。"),
    ]


def finish_revision(store: SessionStore, session: Session) -> None:
    store.db.execute("UPDATE demo_progress SET step=3 WHERE session_id=?", (session.session_id,))


async def advance(
    access: Callable[[], AbstractContextManager[SessionStore]],
    session: Session,
    trace: TraceWriter,
    clock: Clock,
) -> None:
    options = AgentConfig(proposal_gate=True)
    with access() as store:
        state = progress(store, session.session_id)
        if state is None or state.step >= state.total:
            return
        service = Services(store.db, store.clock)
        address = service.get_order("O1006").address.model_dump()
        profile = service.get_user("U005" if state.scenario == 3 else "U006")
    address["detail"] = "公司88号"
    edit = {"tool_name": "modify_address", "arguments": {"order_id": "O1006", "address": address}}
    price = {
        "tool_name": "apply_price_protection",
        "arguments": {"order_id": "O1006", "order_item_id": "OI00006"},
    }
    refund = {
        "order_id": "O1005",
        "items": [{"order_item_id": "OI00005", "qty": 3}],
        "reason": "无理由退货",
    }

    async def turn(text: str, script: list[LLMResponse]) -> None:
        agent = build_async_agent(access, clock, MockLLM(script), options, trace)
        await agent.respond(session, text)

    def approve(store: SessionStore) -> None:
        proposal = session.proposals[-1]
        if proposal.status == "pending":
            decide_proposal(session, proposal.proposal_id, proposal.version, "approve")
            store.save(session)
        trace.emit(
            "confirm_verdict",
            proposal_id=proposal.proposal_id,
            version=proposal.version,
            source="web_button",
            allowed=True,
            rule_ids=[],
        )
        ctx = ToolContext(session, options, store.db, store.clock)
        execute_proposal(ctx, proposal.proposal_id, proposal.version, trace=trace)

    if state.step == 0:
        script = [using("find_user_by_phone", phone=profile.phone, name=profile.name)]
        if state.scenario == 3:
            script += [
                using("get_order", order_id="O1006"),
                using(
                    "propose_actions",
                    actions=[{"tool_name": "request_return", "arguments": refund}],
                ),
                LLMResponse(text="O1005退3件试算¥450.00，需要P08大额审批，不能直接退款。"),
            ]
        else:
            script += [
                using("propose_actions", actions=[edit, price] if state.scenario == 1 else [price]),
                LLMResponse(
                    text="O1006："
                    + ("改送公司88号，并" if state.scenario == 1 else "")
                    + "原路退差¥57.00，请确认整份方案。"
                ),
            ]
        await turn(
            f"我是{profile.name}，电话{profile.phone}。请办理演示所示方案，先复述再确认。", script
        )
    elif state.scenario == 1:
        if state.step == 1:
            if session.proposals[-1].status == "consumed":
                # A direct card decision chose the original complete plan instead of the script.
                with access() as store:
                    store.db.execute(
                        "UPDATE demo_progress SET step=4 WHERE session_id=?", (session.session_id,)
                    )
                return
            tampered = {**address, "detail": "被篡改的地址"}
            await turn(
                "确认",
                [
                    using("modify_address", order_id="O1006", address=tampered),
                    using("cancel_order", order_id="O1006", reason="夹带取消"),
                    LLMResponse(text="两次调用均被P02拦截，未执行任何写入。原方案仍待处理。"),
                ],
            )
        elif state.step == 2:
            amendment = "只改地址，先不退差"
            with access() as store:
                script = revision_script(store, session, amendment)
            assert script is not None
            await turn(amendment, script)
            with access() as store:
                finish_revision(store, session)
            return
        else:
            with access() as store:
                approve(store)
            session._store = None
    elif state.scenario == 2:
        with access() as store:
            if state.step == 1:
                approve(store)
                # Deliberately discard the in-memory response after the real transaction commits.
                restored = store.load(session.session_id, attach=False)
                session.restore_checkpoint(restored)
                trace.emit(
                    "recovery",
                    status="response_discarded_after_commit",
                    session_id=session.session_id,
                )
            else:
                ctx = ToolContext(session, options, store.db, store.clock)
                proposal = session.proposals[-1]
                action = proposal.actions[0]
                result = execute_action(
                    ctx,
                    proposal.proposal_id,
                    action.action_id,
                    proposal.version,
                    action.tool_name,
                    action.canonical_args,
                )
                trace.emit(
                    "recovery",
                    status="replayed",
                    result=result.model_dump(mode="json"),
                    refunds_count=store.db.execute(
                        "SELECT count(*) FROM refunds WHERE order_id='O1006'"
                    ).fetchone()[0],
                )
                try:
                    execute_action(
                        ctx,
                        proposal.proposal_id,
                        action.action_id,
                        proposal.version,
                        action.tool_name,
                        {"order_id": "O1006", "order_item_id": "OI00007"},
                    )
                except PolicyError as exc:
                    trace.emit(
                        "confirm_verdict",
                        allowed=False,
                        rule_ids=exc.rule_ids,
                        reason=str(exc),
                        source="idempotency",
                        proposal_id=proposal.proposal_id,
                        action_id=action.action_id,
                    )
        session._store = None
    else:
        await turn(
            "确认",
            [
                using("request_return", **refund),
                using(
                    "submit_approval",
                    order_id="O1005",
                    action="request_return",
                    args=refund,
                    reason="退款450元超过200元上限",
                ),
                LLMResponse(text="P08已拒绝直退，已生成AP0001（pending），尚未退款。"),
            ],
        )
    session.messages.append(
        Message(role="assistant", content=f"[离线演示] {LABELS[state.scenario][state.step]}")
    )
    with access() as store:
        store.save(session)
        store.db.execute(
            "UPDATE demo_progress SET step=step+1 WHERE session_id=?", (session.session_id,)
        )
    session._store = None

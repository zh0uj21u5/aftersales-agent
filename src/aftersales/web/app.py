"""Thin, synchronous local API; business rules remain in core/policy/domain."""

import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from aftersales.core.config import AgentConfig
from aftersales.core.execution import ActionExecution, execute_proposal, list_executions
from aftersales.core.llm import LLMClient
from aftersales.core.loop import Session, TurnResult
from aftersales.core.runtime import build_agent
from aftersales.core.session_store import SessionConflict, SessionStore
from aftersales.core.tracing import TraceEvent, TraceWriter, read_trace, trace_path
from aftersales.domain.clock import Clock, FixedClock
from aftersales.domain.db import connect
from aftersales.domain.models import BusinessSnapshot, Violation
from aftersales.domain.seed import seed_database
from aftersales.domain.services import PolicyError, Services
from aftersales.policy.proposal import decide_proposal
from aftersales.tools.registry import ToolContext
from aftersales.web.demos import DemoProgress, advance, progress, revise


class CreateSession(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    authenticated_user_id: str | None = None


class SendMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content: str = Field(min_length=1, max_length=20000)


class ProposalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=1)
    decision: Literal["approve", "reject"]
    execute: bool = True


class SessionTraceEvent(TraceEvent):
    run_id: str


class SessionView(Session):
    orders: list[BusinessSnapshot]
    action_executions: list[ActionExecution]
    trace_events: list[SessionTraceEvent]
    execution_notices: list[Violation]
    user_name: str | None = None
    approvals: list[dict[str, JsonValue]] = Field(default_factory=list)
    demo: DemoProgress | None = None


class MessageResponse(BaseModel):
    turn: TurnResult
    session: SessionView


class ReportSummary(BaseModel):
    run_id: str


def create_app(
    db_path: str | Path,
    llm: LLMClient,
    config: AgentConfig | None = None,
    *,
    clock: Clock | None = None,
    trace_dir: str | Path = "traces",
    report_dir: str | Path = "evals/reports",
) -> FastAPI:
    """Inject a client explicitly; constructing or reading the app never calls a model."""
    database = Path(db_path)
    if not database.is_file():
        raise ValueError("数据库不存在，请先 seed")
    options = config or AgentConfig(proposal_gate=True)
    if not (options.guard and options.confirm_gate and options.proposal_gate):
        raise ValueError("本地确认卡 API 必须开启 guard、confirm_gate 与 proposal_gate")
    business_clock = clock or FixedClock()
    lock = RLock()
    app = FastAPI(title="售后管家 · 执行审计工作台")
    web_dir = Path(__file__).parent
    static = web_dir / "static"
    report_sources = (Path(report_dir), web_dir / "samples")
    app.mount("/static", StaticFiles(directory=static), name="static")
    demos = database.parent / (database.stem + "-demos")

    @app.get("/", include_in_schema=False)
    def homepage() -> FileResponse:
        return FileResponse(static / "index.html")

    @contextmanager
    def access(session_id: str | None = None) -> Iterator[SessionStore]:
        # Each route opens and closes its connection in the same worker thread.
        with lock:
            target = database
            if session_id and re.fullmatch(r"[a-f0-9]{32}", session_id):
                candidate = demos / f"{session_id}.db"
                if candidate.is_file() and candidate.resolve().parent == demos.resolve():
                    target = candidate
            db = connect(target)
            try:
                yield SessionStore(db, business_clock)
            except KeyError as exc:
                raise HTTPException(404, "会话不存在") from exc
            except PolicyError as exc:
                raise HTTPException(409, {"message": str(exc), "rule_ids": exc.rule_ids}) from exc
            except SessionConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            finally:
                db.close()

    def view(store: SessionStore, session: Session) -> SessionView:
        service = Services(store.db, business_clock)
        orders = (
            [
                service.action_snapshot(o.id)
                for o in service.list_orders(session.authenticated_user_id)
            ]
            if session.authenticated_user_id
            else []
        )
        events = []
        for run_id in store.trace_ids(session.session_id):
            path = trace_path(trace_dir, run_id)
            if path.is_file():
                events.extend(
                    SessionTraceEvent(run_id=run_id, **event.model_dump())
                    for event in read_trace(path)
                )
        notices = [
            Violation(rule_id="P08", reason="该动作需要人工审批，确认方案不会直接执行退款")
            for p in session.proposals
            if p.status in {"pending", "approved"}
            for a in p.actions
            if a.requires_approval
        ]
        return SessionView(
            **session.model_dump(),
            orders=orders,
            action_executions=list_executions(store, session.session_id),
            trace_events=events,
            execution_notices=notices,
            user_name=service.get_user(session.authenticated_user_id).name
            if session.authenticated_user_id
            else None,
            approvals=[
                dict(row)
                for row in store.db.execute(
                    "SELECT * FROM approvals WHERE user_id=? ORDER BY id",
                    (session.authenticated_user_id,),
                )
            ],
            demo=progress(store, session.session_id),
        )

    @app.post("/api/demos/{scenario}", response_model=SessionView, status_code=201)
    def new_demo(scenario: int) -> SessionView:
        if scenario not in {1, 2, 3}:
            raise HTTPException(404, "演示不存在")
        with lock:
            session = Session()
            demos.mkdir(parents=True, exist_ok=True)
            seed_database(demos / f"{session.session_id}.db")
            with access(session.session_id) as store:
                store.save(session)
                progress(store, session.session_id)
                store.db.execute(
                    "INSERT INTO demo_progress VALUES (?, ?, 0)", (session.session_id, scenario)
                )
                return view(store, session)

    @app.post("/api/sessions/{session_id}/demo/advance", response_model=SessionView)
    def demo_step(session_id: str) -> SessionView:
        with access(session_id) as store:
            session = store.load(session_id)
            if progress(store, session_id) is None:
                raise HTTPException(409, "当前会话不是离线演示")
            writer = TraceWriter(business_clock, trace_dir)
            store.attach_trace(session_id, writer.run_id)
            try:
                advance(store, session, writer)
            finally:
                writer.close("demo_step")
            return view(store, session)

    @app.post("/api/sessions", response_model=SessionView, status_code=201)
    def new_session(body: CreateSession | None = None) -> SessionView:
        body = body or CreateSession()
        with access() as store:
            if body.authenticated_user_id:
                try:
                    Services(store.db, business_clock).get_user(body.authenticated_user_id)
                except ValueError as exc:
                    raise HTTPException(400, "演示用户不存在") from exc
            session = Session(authenticated_user_id=body.authenticated_user_id)
            store.save(session)
            return view(store, session)

    @app.get("/api/sessions/{session_id}", response_model=SessionView)
    def get_session(session_id: str) -> SessionView:
        with access(session_id) as store:
            return view(store, store.load(session_id))

    @app.post("/api/sessions/{session_id}/messages", response_model=MessageResponse)
    def message(session_id: str, body: SendMessage) -> MessageResponse:
        if not body.content.strip():
            raise HTTPException(400, "消息不能为空")
        with access(session_id) as store:
            session = store.load(session_id)
            writer = TraceWriter(business_clock, trace_dir)
            store.attach_trace(session_id, writer.run_id)
            reason = "error"
            try:
                if revise(store, session, writer, body.content):
                    turn = TurnResult(
                        reply=session.messages[-1].content or "", stop_reason="end_turn"
                    )
                else:
                    agent = build_agent(store.db, business_clock, llm, options, writer)
                    turn = agent.respond(session, body.content)
                reason = turn.stop_reason
            finally:
                writer.close(reason)
            return MessageResponse(turn=turn, session=view(store, session))

    @app.post(
        "/api/sessions/{session_id}/proposals/{proposal_id}/decision", response_model=SessionView
    )
    def decision(session_id: str, proposal_id: str, body: ProposalDecision) -> SessionView:
        with access(session_id) as store:
            session = store.load(session_id)
            proposal = next((p for p in session.proposals if p.proposal_id == proposal_id), None)
            if proposal is None or proposal.version != body.version:
                raise HTTPException(409, "方案标识或版本不匹配")
            writer = TraceWriter(business_clock, trace_dir)
            store.attach_trace(session_id, writer.run_id)
            reason = "error"
            try:
                if proposal.status == "pending":
                    decide_proposal(
                        session, proposal_id, body.version, body.decision, source="web_button"
                    )
                elif not (
                    body.decision == "approve"
                    and proposal.status in {"approved", "consumed"}
                    and proposal.confirm_source == "web_button"
                ):
                    raise HTTPException(409, "方案已拒绝、失效或不能重复决策")
                writer.emit(
                    "confirm_verdict",
                    proposal_id=proposal_id,
                    version=body.version,
                    source="web_button",
                    allowed=body.decision == "approve",
                )
                if body.decision == "approve" and body.execute:
                    execute_proposal(
                        ToolContext(session, options, store.db, business_clock),
                        proposal_id,
                        body.version,
                        trace=writer,
                    )
                reason = "completed"
            finally:
                writer.close(reason)
            return view(store, session)

    def report_path(run_id: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", run_id):
            raise HTTPException(404, "报告不存在")
        for directory in report_sources:
            path = directory / f"{run_id}.json"
            if path.is_file() and path.resolve().parent == directory.resolve():
                return path
        raise HTTPException(404, "报告不存在")

    @app.get("/api/evals/reports", response_model=list[ReportSummary])
    def reports() -> list[ReportSummary]:
        result = []
        run_ids = {path.stem for directory in report_sources for path in directory.glob("*.json")}
        for run_id in sorted(run_ids):
            try:
                report_path(run_id)
            except HTTPException:
                continue
            result.append(ReportSummary(run_id=run_id))
        return result

    @app.get("/api/evals/reports/{run_id}", response_model=dict[str, JsonValue])
    def report(run_id: str) -> dict[str, JsonValue]:
        try:
            data = json.loads(report_path(run_id).read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise HTTPException(400, "报告 JSON 无法读取") from exc
        if not isinstance(data, dict):
            raise HTTPException(400, "报告必须是 JSON 对象")
        return data

    return app

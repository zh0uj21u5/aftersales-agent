"""Local async HTTP gateway; business rules remain in core/policy/domain."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    aclosing,
    asynccontextmanager,
    contextmanager,
)
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeVar, cast

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from starlette.types import Receive, Scope, Send

from aftersales.core.config import AgentConfig
from aftersales.core.execution import ActionExecution, execute_proposal, list_executions
from aftersales.core.llm import Client, LLMResponse, MockLLM
from aftersales.core.locks import InMemorySessionLock, SessionBusy
from aftersales.core.loop import AsyncAgent, Session, StreamEvent, TurnResult
from aftersales.core.runtime import build_async_agent, build_system
from aftersales.core.session_store import SessionConflict, SessionStore
from aftersales.core.tracing import TraceEvent, TraceSink, TraceWriter, iter_trace, trace_path
from aftersales.domain.clock import Clock, FixedClock, WallClock
from aftersales.domain.db import connect, ensure_read_indexes
from aftersales.domain.models import BusinessSnapshot, Violation
from aftersales.domain.seed import seed_database
from aftersales.domain.services import PolicyError, Services
from aftersales.policy.confirmation import LEGACY_CONFIRMATION_FIELDS
from aftersales.policy.proposal import decide_proposal
from aftersales.tools.registry import ToolContext
from aftersales.web.demos import (
    DemoProgress,
    advance,
    finish_revision,
    initialize_demos,
    progress,
    revision_script,
)

if TYPE_CHECKING:
    from aftersales.core.llm_router import ModelRouter
    from aftersales.core.pg_sessions import PgSessionStore
    from aftersales.core.telemetry import PgTraceSink
    from aftersales.domain.gateways import FulfillmentGateway, PaymentGateway
    from aftersales.web.production import ProductionSettings
    from aftersales.web.security import Principal

T = TypeVar("T")
LOGGER = logging.getLogger(__name__)
HEARTBEAT_SECONDS = 15
TRACE_PAGE_SIZE = 200
# Leave room for every event in a bounded run while keeping cursor values JS integers.
TRACE_RUN_STRIDE = 1_000_000


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
    run_seq: int


class TracePage(BaseModel):
    events: list[SessionTraceEvent]
    next_seq: int
    has_more: bool


class SessionView(Session):
    orders: list[BusinessSnapshot]
    action_executions: list[ActionExecution]
    trace_events: list[SessionTraceEvent]
    presented_proposal_version: int | None = None
    confirmation_user_index: int
    confirmation_available: bool
    confirmation_checked: bool
    confirmation_identity: str | None
    confirmation_proposal: str
    confirmed_batch: bool
    execution_notices: list[Violation]
    user_name: str | None = None
    approvals: list[dict[str, JsonValue]] = Field(default_factory=list)
    notifications: list[dict[str, JsonValue]] = Field(default_factory=list)
    demo: DemoProgress | None = None


class MessageResponse(BaseModel):
    turn: TurnResult
    session: SessionView


class ReportSummary(BaseModel):
    run_id: str


@dataclass
class _Turn:
    session: Session
    agent: AsyncAgent
    writer: TraceSink
    trace_run: tuple[int, str]
    revised: bool


class TurnStreamResponse(StreamingResponse):
    """Own the mutation lease until EOF, send failure, or receive-side disconnect."""

    def __init__(
        self,
        body: AsyncGenerator[str, None],
        lease: AbstractAsyncContextManager[None],
    ) -> None:
        super().__init__(
            body,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
        self.lease = lease
        self.frames = body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with self.lease:
            writer = asyncio.create_task(self.stream_response(send))
            listener = asyncio.create_task(self.listen_for_disconnect(receive))
            try:
                done, _ = await asyncio.wait(
                    (writer, listener), return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    try:
                        task.result()
                    except OSError:
                        if task is not writer:
                            raise
            finally:
                writer.cancel()
                listener.cancel()
                await asyncio.gather(writer, listener, return_exceptions=True)
                await self.frames.aclose()


def _sse(event: StreamEvent) -> str:
    return f"event: {event.event}\ndata: {json.dumps(event.data, ensure_ascii=False)}\n\n"


def create_app(
    db_path: str | Path | None,
    llm: Client,
    config: AgentConfig | None = None,
    *,
    clock: Clock | None = None,
    trace_dir: str | Path = "traces",
    report_dir: str | Path = "evals/reports",
    production: ProductionSettings | None = None,
    payment_gateway: PaymentGateway | None = None,
    fulfillment_gateway: FulfillmentGateway | None = None,
    model_router: ModelRouter | None = None,
) -> FastAPI:
    """Inject a client explicitly; constructing or reading the app never calls a model."""
    database = Path(db_path) if db_path is not None else None
    if production is None and (database is None or not database.is_file()):
        raise ValueError("数据库不存在，请先 seed")
    options = config or AgentConfig(proposal_gate=True, output_guard=production is not None)
    if not (options.guard and options.confirm_gate and options.proposal_gate):
        raise ValueError("本地确认卡 API 必须开启 guard、confirm_gate 与 proposal_gate")
    business_clock = clock or (WallClock() if production is not None else FixedClock())
    prod = None
    if production is not None:
        from aftersales.web.production import Production, ProductionMiddleware

        prod = Production(
            production,
            business_clock,
            payment=payment_gateway,
            fulfillment=fulfillment_gateway,
        )
    locks = prod.coordinator if prod is not None else InMemorySessionLock()
    system_prompt: str | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal system_prompt
        try:
            if prod is None:
                yield
            else:
                async with prod.lifespan():
                    system_prompt = await asyncio.to_thread(
                        build_system, business_clock, options.policy_mode, stable=True
                    )
                    system_prompt += (
                        "\n生产操作结果 status=processing 表示已受理并等待外部渠道确认。"
                        "不要声称退款已到账、地址已同步或仓库已拦截。"
                    )
                    yield
        finally:
            if model_router is not None:
                await model_router.aclose()
            elif close := getattr(llm, "aclose", None):
                await close()

    app = FastAPI(lifespan=lifespan, title="售后管家 · 执行审计工作台")
    if prod is not None:
        from aftersales.web.admin import router as admin_router
        from aftersales.web.webhooks import router as webhook_router

        app.add_middleware(ProductionMiddleware, production=prod)
        app.include_router(admin_router(prod))
        app.include_router(webhook_router(prod))
    web_dir = Path(__file__).parent
    static = web_dir / "static"
    report_sources = (Path(report_dir), web_dir / "samples")
    app.mount("/static", StaticFiles(directory=static), name="static")
    demos = database.parent / (database.stem + "-demos") if database is not None else None
    prepared_databases: set[Path] = set()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        if prod is None:
            return JSONResponse({"status": "ready", "mode": "local"})
        try:
            ready, checks = await prod.readiness()
        except TimeoutError:
            return JSONResponse({"status": "unready", "checks": {"timeout": True}}, status_code=503)
        return JSONResponse(
            {"status": "ready" if ready else "unready", "checks": checks},
            status_code=200 if ready else 503,
        )

    @app.get("/", include_in_schema=False)
    def homepage() -> FileResponse:
        return FileResponse(static / "index.html")

    @asynccontextmanager
    async def mutation(
        session_id: str, actor: Principal | None = None, *, dialogue: bool = False
    ) -> AsyncIterator[None]:
        if prod is not None:
            assert actor is not None
            actor.require("buyer")
            await run_db(lambda store: require_owner(store, session_id, actor), session_id)
        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(locks.acquire(session_id))
                if prod is not None and dialogue:
                    await stack.enter_async_context(prod.coordinator.admit(actor.user_id))
                    budget = prod.budget(actor.user_id)
                    if model_router is None:
                        await budget.before(options.model)
                    else:
                        await budget.check_balance()
                yield
        except SessionBusy as exc:
            raise HTTPException(409, "当前会话正在处理上一条消息") from exc
        except Exception as exc:
            if prod is not None:
                from redis.exceptions import RedisError

                from aftersales.core.llm import InferenceUnavailable
                from aftersales.core.redis_coord import AdmissionDenied, CoordinationLost
                from aftersales.web.rate_limit import coordination_error

                if isinstance(
                    exc, (RedisError, AdmissionDenied, CoordinationLost, InferenceUnavailable)
                ):
                    raise coordination_error(exc) from exc
            raise

    def database_for(session_id: str | None) -> Path:
        assert database is not None and demos is not None
        if session_id and re.fullmatch(r"[a-f0-9]{32}", session_id):
            candidate = demos / f"{session_id}.db"
            if candidate.is_file() and candidate.resolve().parent == demos.resolve():
                return candidate
        return database

    @contextmanager
    def access(session_id: str | None = None) -> Iterator[SessionStore]:
        target = database_for(session_id)
        db = connect(target)
        try:
            initialize = target not in prepared_databases
            store = SessionStore(db, business_clock, initialize=initialize)
            if initialize:
                ensure_read_indexes(db)
                initialize_demos(store)
                prepared_databases.add(target)
            yield store
        except KeyError as exc:
            raise HTTPException(404, "会话不存在") from exc
        except PolicyError as exc:
            raise HTTPException(409, {"message": str(exc), "rule_ids": exc.rule_ids}) from exc
        except SessionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            db.close()

    if prod is None:
        with access():
            pass

    async def run_db(work: Callable[[SessionStore], T], session_id: str | None = None) -> T:
        if prod is None:
            with access(session_id) as store:
                return work(store)
        try:
            return await prod.run(work)
        except KeyError as exc:
            raise HTTPException(404, "会话不存在") from exc
        except PolicyError as exc:
            raise HTTPException(409, {"message": str(exc), "rule_ids": exc.rule_ids}) from exc
        except SessionConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    def principal(request: Request) -> Principal | None:
        return request.state.principal if prod is not None else None

    def require_owner(store: SessionStore, session_id: str, actor: Principal | None) -> None:
        if actor is None:
            store.require_session(session_id)
        else:
            cast("PgSessionStore", store).require_owner(session_id, actor.user_id)

    async def claim_revision(session_id: str, actor: Principal | None) -> Session | None:
        if prod is None:
            return None

        def load(store: SessionStore) -> Session:
            require_owner(store, session_id, actor)
            return store.load(session_id, attach=False)

        session = await run_db(load, session_id)
        # Check the lease after loading, then advance only the revision we loaded.
        await prod.coordinator.require_current_lease(session_id)
        await run_db(lambda store: store.save(session), session_id)
        session._store = None
        return session

    def new_writer(session_id: str) -> TraceSink:
        return (
            prod.writer(session_id) if prod is not None else TraceWriter(business_clock, trace_dir)
        )

    def attach_writer(store: SessionStore, session_id: str, writer: TraceSink) -> int:
        sequence = store.attach_trace(session_id, writer.run_id)
        if prod is not None:
            cast("PgTraceSink", writer).run_number = sequence
        return sequence

    async def flush(writer: TraceSink) -> None:
        if prod is not None:
            await cast("PgTraceSink", writer).flush()

    def pg_events(
        store: SessionStore,
        session_id: str,
        *,
        after_seq: int = 0,
        limit: int = TRACE_PAGE_SIZE,
        newest: bool = False,
        run_number: int | None = None,
    ) -> list[SessionTraceEvent]:
        assert prod is not None
        return [
            SessionTraceEvent(
                **{**event.model_dump(), "seq": (number - 1) * TRACE_RUN_STRIDE + event.seq},
                run_id=run_id,
                run_seq=event.seq,
            )
            for number, run_id, event in prod.audit_events(
                cast("PgSessionStore", store),
                session_id,
                after_seq=after_seq,
                limit=limit,
                newest=newest,
                run_number=run_number,
            )
        ]

    def run_events(run: tuple[int, str], *, after_seq: int = 0) -> Iterator[SessionTraceEvent]:
        run_number, run_id = run
        path = trace_path(trace_dir, run_id)
        if path.is_file():
            offset = (run_number - 1) * TRACE_RUN_STRIDE
            for event in iter_trace(path, after_seq=max(after_seq - offset, 0), complete_only=True):
                yield SessionTraceEvent(
                    **{**event.model_dump(), "seq": offset + event.seq},
                    run_id=run_id,
                    run_seq=event.seq,
                )

    def recent_events(store: SessionStore, session_id: str) -> list[SessionTraceEvent]:
        if prod is not None:
            return pg_events(store, session_id, newest=True)
        events: deque[SessionTraceEvent] = deque(maxlen=TRACE_PAGE_SIZE)
        for run in store.trace_runs(session_id, newest_first=True, limit=TRACE_PAGE_SIZE):
            capacity = TRACE_PAGE_SIZE - len(events)
            if not capacity:
                break
            tail = deque(run_events(run), maxlen=capacity)
            events.extendleft(reversed(tail))
        return list(events)

    def view(
        store: SessionStore, session: Session, *, events: list[SessionTraceEvent] | None = None
    ) -> SessionView:
        service = Services(store.db, business_clock)
        user_id = session.authenticated_user_id
        ids = (
            []
            if user_id is None
            else [
                row[0]
                for row in store.db.execute(
                    "SELECT id FROM orders WHERE user_id=? ORDER BY id",
                    (user_id,),
                )
            ]
        )
        orders = service.batch_action_snapshots(ids)
        for snapshot in orders:
            for item in snapshot.order_items:
                returned, qty = item["returned_qty"], item["qty"]
                item["is_partially_returned"] = 0 < returned < qty
        notices = [
            Violation(rule_id="P08", reason="该动作需要人工审批，确认方案不会直接执行退款")
            for p in session.proposals
            if p.status in {"pending", "approved"}
            for a in p.actions
            if a.requires_approval
        ]
        return SessionView(
            **session.model_dump(),
            **{key: getattr(session, key) for key in LEGACY_CONFIRMATION_FIELDS},
            orders=orders,
            action_executions=list_executions(store, session.session_id),
            trace_events=recent_events(store, session.session_id) if events is None else events,
            execution_notices=notices,
            user_name=service.get_user(session.authenticated_user_id).name
            if session.authenticated_user_id
            else None,
            approvals=[]
            if user_id is None
            else [
                dict(row)
                for row in store.db.execute(
                    "SELECT * FROM approvals WHERE user_id=? ORDER BY id",
                    (user_id,),
                )
            ],
            demo=progress(store, session.session_id) if prod is None else None,
            notifications=[]
            if prod is None
            else [
                dict(row)
                for row in store.db.execute(
                    """SELECT id, message, created_at FROM session_notifications
                       WHERE session_id=? ORDER BY id DESC LIMIT 50""",
                    (session.session_id,),
                )
            ],
        )

    @app.post("/api/demos/{scenario}", response_model=SessionView, status_code=201)
    def new_demo(scenario: int) -> SessionView:
        if prod is not None or demos is None or scenario not in {1, 2, 3}:
            raise HTTPException(404, "演示不存在")
        session = Session()
        demos.mkdir(parents=True, exist_ok=True)
        seed_database(demos / f"{session.session_id}.db")
        with access(session.session_id) as store:
            store.save(session)
            store.db.execute(
                "INSERT INTO demo_progress VALUES (?, ?, 0)", (session.session_id, scenario)
            )
            return view(store, session)

    @app.post("/api/sessions/{session_id}/demo/advance", response_model=SessionView)
    async def demo_step(session_id: str) -> SessionView:
        if prod is not None:
            raise HTTPException(404, "演示不存在")
        async with mutation(session_id):
            with access(session_id) as store:
                session = store.load(session_id, attach=False)
                if progress(store, session_id) is None:
                    raise HTTPException(409, "当前会话不是离线演示")
                writer = TraceWriter(business_clock, trace_dir)
                store.attach_trace(session_id, writer.run_id)
            try:
                await advance(lambda: access(session_id), session, writer, business_clock)
            finally:
                writer.close("demo_step")
            with access(session_id) as store:
                return view(store, session)

    @app.post("/api/sessions", response_model=SessionView, status_code=201)
    async def new_session(request: Request, body: CreateSession | None = None) -> SessionView:
        body = body or CreateSession()
        actor = principal(request)
        if actor is not None:
            actor.require("buyer")
            if "authenticated_user_id" in body.model_fields_set:
                raise HTTPException(403, "会话身份由登录令牌绑定")
        user_id = actor.user_id if actor is not None else body.authenticated_user_id

        def create(store: SessionStore) -> SessionView:
            if user_id:
                try:
                    Services(store.db, business_clock).get_user(user_id)
                except ValueError as exc:
                    status = 403 if actor is not None else 400
                    raise HTTPException(status, "用户不存在") from exc
            session = Session(authenticated_user_id=user_id)
            if actor is not None:
                session._owner_user_id = actor.user_id
            store.save(session)
            return view(store, session)

        return await run_db(create)

    @app.get("/api/sessions/{session_id}", response_model=SessionView)
    async def get_session(session_id: str, request: Request) -> SessionView:
        if prod is not None:
            try:
                session = await prod.snapshot(session_id, principal(request))
            except KeyError as exc:
                raise HTTPException(404, "会话不存在") from exc
            return await run_db(lambda store: view(store, session), session_id)
        return await run_db(lambda store: view(store, store.load(session_id)), session_id)

    @app.get("/api/sessions/{session_id}/traces", response_model=TracePage)
    async def traces(
        session_id: str,
        request: Request,
        after_seq: int = Query(default=0, ge=0, le=9_007_199_254_740_991),
        limit: int = Query(default=TRACE_PAGE_SIZE, ge=1, le=1000),
    ) -> TracePage:
        actor = principal(request)

        def page(store: SessionStore) -> TracePage:
            require_owner(store, session_id, actor)
            events = pg_events(store, session_id, after_seq=after_seq, limit=limit + 1)
            return TracePage(
                events=events[:limit],
                next_seq=events[min(len(events), limit) - 1].seq if events else after_seq,
                has_more=len(events) > limit,
            )

        if prod is not None:
            return await run_db(page, session_id)

        def runs_from_cursor(store: SessionStore) -> list[tuple[int, str]]:
            store.require_session(session_id)
            return store.trace_runs(session_id, first_run=after_seq // TRACE_RUN_STRIDE + 1)

        runs = await run_db(runs_from_cursor, session_id)
        events = []
        for run in runs:
            for event in run_events(run, after_seq=after_seq):
                if len(events) == limit:
                    return TracePage(events=events, next_seq=events[-1].seq, has_more=True)
                events.append(event)
        return TracePage(
            events=events, next_seq=events[-1].seq if events else after_seq, has_more=False
        )

    @asynccontextmanager
    async def start_turn(
        session_id: str, content: str, actor: Principal | None
    ) -> AsyncIterator[_Turn]:
        writer: TraceSink | None = None
        reason = "error"
        claimed = await claim_revision(session_id, actor)

        def prepare(store: SessionStore) -> tuple[Session, list[LLMResponse] | None, int]:
            nonlocal writer
            session = claimed if claimed is not None else store.load(session_id, attach=False)
            script = revision_script(store, session, content) if prod is None else None
            writer = new_writer(session_id)
            run_number = attach_writer(store, session_id, writer)
            return session, script, run_number

        try:
            session, script, run_number = await run_db(prepare, session_id)
            current_writer = writer
            assert current_writer is not None

            async def production_scope(work: Callable[[SessionStore], T]) -> T:
                value = await run_db(work, session_id)
                await flush(current_writer)
                return value

            agent = build_async_agent(
                (lambda: access(session_id)) if prod is None else None,
                business_clock,
                MockLLM(script) if script else llm,
                AgentConfig(proposal_gate=True) if script else options,
                current_writer,
                run_store=production_scope if prod is not None else None,
                budget=prod.budget(actor.user_id)
                if prod is not None and actor is not None
                else None,
                system_prompt=system_prompt,
                router=model_router,
            )
            yield _Turn(
                session, agent, current_writer, (run_number, current_writer.run_id), bool(script)
            )
            reason = "completed"
        except (asyncio.CancelledError, GeneratorExit):
            reason = "disconnected"
            raise
        finally:
            if writer is not None:
                writer.close(reason)
                await flush(writer)

    async def message_view(turn: _Turn) -> SessionView:
        session_id = turn.session.session_id
        await flush(turn.writer)

        def project(store: SessionStore) -> SessionView:
            if turn.revised:
                finish_revision(store, turn.session)
            events = (
                pg_events(store, session_id, run_number=turn.trace_run[0])
                if prod is not None
                else list(run_events(turn.trace_run))
            )
            return view(store, turn.session, events=events)

        return await run_db(project, session_id)

    @app.post("/api/sessions/{session_id}/messages", response_model=MessageResponse)
    async def message(session_id: str, body: SendMessage, request: Request) -> MessageResponse:
        if not body.content.strip():
            raise HTTPException(400, "消息不能为空")
        actor = principal(request)
        async with (
            mutation(session_id, actor, dialogue=True),
            start_turn(session_id, body.content, actor) as current,
        ):
            result = await current.agent.respond(current.session, body.content)
            current.writer.close(result.stop_reason)
            return MessageResponse(turn=result, session=await message_view(current))

    async def frames(
        session_id: str, content: str, actor: Principal | None
    ) -> AsyncGenerator[str, None]:
        try:
            async with (
                start_turn(session_id, content, actor) as current,
                aclosing(current.agent.respond_stream(current.session, content)) as events,
            ):
                pending: asyncio.Task[StreamEvent] | None = None
                try:
                    while True:
                        pending = pending or asyncio.create_task(anext(events))
                        done, _ = await asyncio.wait((pending,), timeout=HEARTBEAT_SECONDS)
                        if not done:
                            yield ": keep-alive\n\n"
                            continue
                        try:
                            event = pending.result()
                        except StopAsyncIteration:
                            break
                        pending = None
                        if event.event == "turn_end":
                            current.writer.close(str(event.data["stop_reason"]))
                            state = await message_view(current)
                            event.data = {
                                "turn": event.data,
                                "session": state.model_dump(mode="json"),
                            }
                        yield _sse(event)
                finally:
                    if pending is not None:
                        pending.cancel()
                        await asyncio.gather(pending, return_exceptions=True)
        except HTTPException as exc:
            yield _sse(StreamEvent(event="error", data={"message": exc.detail}))
        except Exception as exc:  # noqa: BLE001 - SSE headers have already been sent.
            LOGGER.error("Streaming turn failed (%s)", type(exc).__name__)
            yield _sse(
                StreamEvent(event="error", data={"message": "本轮处理失败，请刷新会话后重试。"})
            )

    @app.post("/api/sessions/{session_id}/messages/stream", response_class=StreamingResponse)
    async def stream_message(
        session_id: str, body: SendMessage, request: Request
    ) -> StreamingResponse:
        if not body.content.strip():
            raise HTTPException(400, "消息不能为空")
        actor = principal(request)
        await run_db(lambda store: require_owner(store, session_id, actor), session_id)
        return TurnStreamResponse(
            frames(session_id, body.content, actor), mutation(session_id, actor, dialogue=True)
        )

    @app.post(
        "/api/sessions/{session_id}/proposals/{proposal_id}/decision", response_model=SessionView
    )
    async def decision(
        session_id: str, proposal_id: str, body: ProposalDecision, request: Request
    ) -> SessionView:
        actor = principal(request)
        writer: TraceSink | None = None
        async with mutation(session_id, actor):
            claimed = await claim_revision(session_id, actor)

            def apply(store: SessionStore) -> Session:
                nonlocal writer
                session = claimed if claimed is not None else store.load(session_id)
                session._store = store
                proposal = next(
                    (p for p in session.proposals if p.proposal_id == proposal_id), None
                )
                if proposal is None or proposal.version != body.version:
                    raise HTTPException(409, "方案标识或版本不匹配")
                writer = new_writer(session_id)
                attach_writer(store, session_id, writer)
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
                return session

            try:
                session = await run_db(apply, session_id)
            finally:
                if writer is not None:
                    await flush(writer)
            session._store = None
            return await run_db(lambda store: view(store, session), session_id)

    def report_path(run_id: str) -> Path:
        if prod is not None or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", run_id):
            raise HTTPException(404, "报告不存在")
        for directory in report_sources:
            path = directory / f"{run_id}.json"
            if path.is_file() and path.resolve().parent == directory.resolve():
                return path
        raise HTTPException(404, "报告不存在")

    @app.get("/api/evals/reports", response_model=list[ReportSummary])
    def reports() -> list[ReportSummary]:
        if prod is not None:
            raise HTTPException(404, "报告不存在")
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

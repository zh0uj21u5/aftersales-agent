"""CLI entry points for seeded conversations, traces and future evaluation."""

import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from aftersales.core.config import AgentConfig, load_settings
from aftersales.core.llm import make_client
from aftersales.core.loop import Agent, Session
from aftersales.core.runtime import build_async_agent
from aftersales.core.session_store import SessionStore
from aftersales.core.tracing import TraceWriter, render_trace, trace_path
from aftersales.domain.clock import FixedClock
from aftersales.domain.db import connect
from aftersales.domain.seed import seed_database
from aftersales.policy.proposal import format_proposal_summary, latest_proposal

app = typer.Typer(help="E-commerce after-sales agent.", no_args_is_help=True)


@app.callback()
def configure_output() -> None:
    """Keep redirected Chinese text and currency portable on Windows."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


@app.command()
def chat(
    db: Annotated[Path, typer.Option(help="Existing seeded SQLite database")] = Path(
        "data/shop.db"
    ),
    config: Annotated[Path | None, typer.Option(help="AgentConfig YAML preset")] = None,
    trace_dir: Annotated[Path, typer.Option(help="Trace output directory")] = Path("traces"),
    confirm_model: Annotated[
        str | None, typer.Option(help="Optional small classifier model on the same provider")
    ] = None,
    session_id: Annotated[
        str | None, typer.Option("--session", help="Resume a saved session")
    ] = None,
) -> None:
    """Chat with the agent; enter /exit to finish and close the trace."""
    if not db.is_file():
        raise typer.BadParameter(
            "数据库不存在，请先运行 aftersales seed --db <路径>", param_hint="--db"
        )
    try:
        settings = load_settings()
        overrides = yaml.safe_load(config.read_text(encoding="utf-8")) if config else {}
        if overrides is None:
            overrides = {}
        if (
            not isinstance(overrides, dict)
            or not overrides.keys() <= AgentConfig.model_fields.keys()
        ):
            raise ValueError("配置必须为 AgentConfig 字段的映射")
        options = AgentConfig.model_validate({**settings.agent_config().model_dump(), **overrides})
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise typer.BadParameter("请检查 .env 或配置文件（模型、凭据及配置项）") from exc
    clock = FixedClock()

    @contextmanager
    def access(*, initialize: bool = False) -> Iterator[SessionStore]:
        with closing(connect(db)) as connection:
            yield SessionStore(connection, clock, initialize=initialize)

    writer = None
    reason = "exit"
    try:
        with closing(connect(db)) as connection:
            columns = list(connection.execute("PRAGMA table_info(handoffs)"))
            if not columns or any(row[1] == "user_id" and row[3] for row in columns):
                raise typer.BadParameter("数据库 schema 早于 P3，请用 seed --db <新路径> 生成新库")
        llm = make_client(options.provider, options.model)
        classifier = make_client(options.provider, confirm_model) if confirm_model else None
        with access(initialize=True) as store:
            if session_id is not None:
                try:
                    session = store.load(session_id, attach=False)
                except KeyError as exc:
                    raise typer.BadParameter("会话不存在", param_hint="--session") from exc
            else:
                session = Session()
                store.save(session)
                session._store = None
            writer = TraceWriter(clock, trace_dir)
            store.attach_trace(session.session_id, writer.run_id)
        agent = Agent(
            engine=build_async_agent(
                access,
                clock,
                llm,
                options,
                writer,
                classifier=classifier,
                classifier_model=confirm_model,
                prompt_cache_pricing=False,
            )
        )
        console = Console()
        console.print(f"售后管家 · run_id={writer.run_id} · 输入 /exit 退出", markup=False)
        console.print(f"session_id={session.session_id}", markup=False)
        while not session.closed:
            try:
                user_text = typer.prompt("你")
            except (EOFError, KeyboardInterrupt, typer.Abort):
                reason = "interrupted"
                break
            if user_text.strip().lower() in {"/exit", "/quit"}:
                break
            if not user_text.strip():
                continue
            result = agent.respond(session, user_text)
            proposal = latest_proposal(session)
            card = (
                format_proposal_summary(proposal)
                if proposal and proposal.status == "pending"
                else None
            )
            reply = result.reply.removesuffix("\n\n" + card) if card else result.reply
            console.print(f"售后管家：{reply}", markup=False)
            if card:
                console.print(Panel(Text(card)))
            if session.closed:
                reason = "handoff"
        console.print(
            f"查看记录：aftersales trace {writer.run_id} --trace-dir {trace_dir}", markup=False
        )
    except BaseException:
        reason = "error"
        raise
    finally:
        if writer:
            writer.close(reason)


@app.command()
def eval(
    tasks: Annotated[Path, typer.Option(help="Task directory")] = Path("evals/tasks"),
    config: Annotated[Path, typer.Option(help="AgentConfig YAML preset")] = Path(
        "configs/baseline.yaml"
    ),
    tags: Annotated[str | None, typer.Option(help="Comma-separated tags, match any")] = None,
    limit: Annotated[int | None, typer.Option(min=1)] = None,
    k: Annotated[int, typer.Option("-k", "--k", min=1, help="pass^k; default trials per task")] = 1,
    trials: Annotated[int | None, typer.Option(min=1, help="Independent trials n >= k")] = None,
    user: Annotated[
        str, typer.Option(help="User mode: scripted or llm (at most 20 turns)")
    ] = "scripted",
    cache: Annotated[bool, typer.Option(help="Replay matching trial cache")] = False,
    model: Annotated[str | None, typer.Option(help="Override the evaluated Agent model")] = None,
    simulator_model: Annotated[
        str | None, typer.Option(help="Simulator model; defaults to MODEL_ID in .env")
    ] = None,
    report_dir: Annotated[Path, typer.Option()] = Path("evals/reports"),
    trace_dir: Annotated[Path, typer.Option()] = Path("traces"),
) -> None:
    """Run fresh dual-database trials and write JSON/Markdown metrics."""
    from evals.models import load_tasks
    from evals.runner import run_suite

    if user not in {"scripted", "llm"}:
        raise typer.BadParameter("--user 必须为 scripted 或 llm")
    try:
        settings = load_settings()
        overrides = yaml.safe_load(config.read_text(encoding="utf-8"))
        if (
            not isinstance(overrides, dict)
            or not overrides.keys() <= AgentConfig.model_fields.keys()
        ):
            raise ValueError("配置必须为 AgentConfig 字段的映射")
        options = AgentConfig.model_validate({**settings.agent_config().model_dump(), **overrides})
        if model:
            options = options.model_copy(update={"model": model})
        selected = load_tasks(tasks)
        if tags:
            wanted = {tag.strip() for tag in tags.split(",") if tag.strip()}
            selected = [task for task in selected if wanted.intersection(task.tags)]
        if limit:
            selected = selected[:limit]
        if not selected or (trials is not None and trials < k):
            raise ValueError("无匹配任务或 trials < k")
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    llm = make_client(options.provider, options.model)
    typer.echo(f"模型 {options.model}；任务 {len(selected)}；每任务 {trials or k} 次；用户 {user}")
    report = run_suite(
        selected,
        llm,
        options,
        k=k,
        n=trials,
        report_dir=report_dir,
        trace_dir=trace_dir,
        cache_dir=Path("data/eval-cache") if cache else None,
        user_mode=user,
        simulator_llm=make_client(options.provider, simulator_model or settings.model)
        if user == "llm"
        else None,
        simulator_model=simulator_model or settings.model,
        progress=lambda result: typer.echo(
            f"{result.task_id} trial={result.trial} pass={result.grade.passed} "
            f"state={result.grade.state_correct} turns={result.turns}"
        ),
    )
    typer.echo(f"pass^1={report.metrics['pass^1']:.3f} pass^{k}={report.metrics[f'pass^{k}']:.3f}")
    typer.echo(f"报告：{report_dir / (report.run_id + '.md')}")


@app.command()
def trace(run_id: str, trace_dir: Annotated[Path, typer.Option()] = Path("traces")) -> None:
    """Render a run's timeline, highlighting denied calls and writes."""
    try:
        render_trace(trace_path(trace_dir, run_id))
    except BrokenPipeError:
        return
    except UnicodeEncodeError as exc:
        raise typer.BadParameter("终端无法显示文本，请使用 UTF-8 输出") from exc
    except (OSError, ValueError, ValidationError) as exc:
        raise typer.BadParameter("trace 不存在、run_id 无效或 JSONL 格式损坏") from exc


@app.command()
def seed(
    db: str = typer.Option("data/shop.db", help="SQLite path"),
    rng_seed: int = typer.Option(42, "--seed", help="Random seed, same seed -> same data"),
) -> None:
    """Generate a fresh deterministic demo database."""
    try:
        seed_database(db, rng_seed)
    except FileExistsError as exc:
        raise typer.BadParameter(str(exc), param_hint="--db") from exc
    typer.echo(f"已生成数据库：{db}（seed={rng_seed}，50 用户 / 120 SKU / 300 订单）")


@app.command()
def serve(
    db: Annotated[Path, typer.Option(help="Existing seeded SQLite database")] = Path(
        "data/shop.db"
    ),
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8000,
    offline: Annotated[bool, typer.Option(help="Offline demos; never load credentials")] = False,
    mode: Annotated[
        Literal["local", "prod"] | None, typer.Option(help="Storage and security mode")
    ] = None,
) -> None:
    """Start the HTTP gateway on loopback."""
    import asyncio
    import os

    import uvicorn
    from dotenv import dotenv_values

    from aftersales.web.app import create_app

    environment = {} if offline else {**dotenv_values(".env"), **os.environ}
    production = None
    configured = False
    if mode is None and not offline:
        configured = any(environment.get(key) for key in ("DATABASE_URL", "REDIS_URL"))
    if mode == "prod" and offline:
        raise typer.BadParameter("生产模式与离线演示不能同时启用", param_hint="--mode")
    if mode == "prod" or configured:
        from aftersales.web.production import ProductionSettings

        production = ProductionSettings.from_env()
    if production is None and not db.is_file():
        raise typer.BadParameter("数据库不存在，请先 seed", param_hint="--db")
    from aftersales.core.llm import LLMResponse, make_async_client

    class OfflineClient:
        async def chat(self, *args: Any, **kwargs: Any) -> LLMResponse:
            return LLMResponse(
                text="当前为纯离线模式。请使用演示场景的下一步或确认卡按钮；自由消息已保存。真实客服对话请不带 --offline 启动。"
            )

    router = None
    routing_path = environment.get("LLM_ROUTING_CONFIG") if production is not None else None
    if offline:
        options = AgentConfig(proposal_gate=True)
        client = OfflineClient()
    elif routing_path:
        from aftersales.core.llm_router import load_router

        router = load_router(routing_path, environment)
        primary = router.chains["agent"][0]
        options = AgentConfig(
            provider=primary.provider,
            model=primary.model,
            proposal_gate=True,
            confirm_mode="llm" if "confirmation" in router.chains else "heuristic",
            goal_judge="goal_judge" in router.chains,
            max_steps=int(environment.get("MAX_STEPS") or 30),
            tool_output_max_chars=int(environment.get("TOOL_OUTPUT_MAX_CHARS") or 8000),
        )
        client = primary.client
    else:
        settings = load_settings()
        options = settings.agent_config().model_copy(update={"proposal_gate": True})
        client = make_async_client(options.provider, options.model)
    gateway = create_app(
        db if production is None else None,
        client,
        options,
        production=production,
        model_router=router,
    )
    if production is not None and sys.platform == "win32":
        server = uvicorn.Server(
            uvicorn.Config(
                gateway,
                host="127.0.0.1",
                port=port,
                timeout_graceful_shutdown=35,
            )
        )
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(server.serve())
    elif production is not None:
        uvicorn.run(gateway, host="127.0.0.1", port=port, timeout_graceful_shutdown=35)
    else:
        uvicorn.run(gateway, host="127.0.0.1", port=port)


@app.command("reconcile")
def reconcile_command(
    hanging_seconds: Annotated[int, typer.Option(min=1)] = 300,
    statement_lag_seconds: Annotated[int, typer.Option(min=0)] = 300,
) -> None:
    """Repair stale refunds and compare the closed channel statement."""
    import asyncio
    import os
    from datetime import timedelta

    from dotenv import dotenv_values

    from aftersales.domain.clock import WallClock
    from aftersales.domain.gateways import GatewaySettings, open_gateways
    from aftersales.domain.reconciliation import reconcile
    from aftersales.domain.storage import PostgresStorage

    values = {**dotenv_values(".env"), **os.environ}
    database = values.get("DATABASE_URL")
    gateways = GatewaySettings.from_values(values)
    if not database or not gateways.payment_url or gateways.sandbox:
        raise typer.BadParameter("需要 DATABASE_URL 和可持久查询的 PAYMENT_GATEWAY_URL")
    clock = WallClock()
    storage = PostgresStorage(database)

    async def run() -> None:
        async with storage.pool, open_gateways(gateways, clock) as (payment, _):
            assert payment is not None
            report = await reconcile(
                storage,
                clock,
                payment,
                hanging_age=timedelta(seconds=hanging_seconds),
                statement_lag=timedelta(seconds=statement_lag_seconds),
            )
        typer.echo(report.model_dump_json())

    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(run())


if __name__ == "__main__":
    app()

"""Run the real harness and SQLite services with deterministic, offline model responses."""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue
from rich.console import Console

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aftersales.core.config import AgentConfig
from aftersales.core.llm import LLMResponse, MockLLM, ToolCall
from aftersales.core.loop import Session
from aftersales.core.runtime import build_agent
from aftersales.core.tracing import TraceWriter, read_trace, render_trace
from aftersales.domain.clock import FixedClock
from aftersales.domain.db import connect, dump_hashes
from aftersales.domain.seed import seed_database
from aftersales.domain.services import Services


@dataclass(frozen=True)
class DemoEvidence:
    scene: str
    database: Path
    trace: Path
    seed_hashes: dict[str, str]
    amount_cents: int
    blocked_rules: tuple[str, ...] = ()


def calls(*operations: tuple[str, dict[str, JsonValue]]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        tool_calls=[
            ToolCall(id=str(i), name=name, arguments=args)
            for i, (name, args) in enumerate(operations)
        ],
    )


def money(cents: int) -> str:
    return f"¥{cents // 100}.{cents % 100:02d}"


def run_demo(output: Path, console: Console) -> list[DemoEvidence]:
    """Create two fresh stores and verify execution, denial and approval evidence."""
    output.mkdir(parents=True, exist_ok=False)
    clock = FixedClock()
    config = AgentConfig(model="mock", provider="mock", preview_tool=True)
    evidence = []
    for scene in ("A", "B"):
        database = output / f"scene-{scene}.db"
        seed_database(database, seed=42, clock=clock)
        db = connect(database)
        writer = TraceWriter(clock, output / "traces")
        try:
            service = Services(db, clock)
            initial = dump_hashes(db)
            console.rule(f"场景 {scene} · seed=42 · {clock.now().isoformat()}")
            if scene == "A":
                address = service.get_order("O1006").address.model_dump()
                address["detail"] = "公司路88号"
                amount = service.preview_refund(
                    "O1006", "apply_price_protection", "OI00006"
                ).amount_cents
                script = [
                    calls(
                        ("find_user_by_phone", {"phone": "13800000006", "name": "用户006"}),
                        ("get_order", {"order_id": "O1006"}),
                        (
                            "preview_refund",
                            {
                                "order_id": "O1006",
                                "action": "apply_price_protection",
                                "order_item_id": "OI00006",
                            },
                        ),
                    ),
                    LLMResponse(
                        text=f"O1006改送公司路88号，收件人和电话不变；价保原路退差{money(amount)}。请确认。"
                    ),
                    calls(
                        ("modify_address", {"order_id": "O1006", "address": address}),
                        (
                            "apply_price_protection",
                            {"order_id": "O1006", "order_item_id": "OI00006"},
                        ),
                    ),
                    LLMResponse(text=f"地址已修改，已按订单原支付方式退差{money(amount)}。"),
                ]
                mock = MockLLM(script)
                agent = build_agent(db, clock, mock, config, writer)
                session = Session()
                console.print("用户：认证后请改地址并办理价保，先复述方案等确认。")
                proposal = agent.respond(
                    session,
                    "我是用户006，13800000006；O1006改送公司路88号，同时为OI00006办理价保，先等我确认。",
                )
                console.print("助手：" + proposal.reply)
                if dump_hashes(db) != initial:
                    raise RuntimeError("Proposal unexpectedly changed the database")
                console.print("检查：确认之前数据库没有变化。")
                console.print("用户：好的，确认办理，不取消订单。")
                result = agent.respond(session, "好的，确认办理，不取消订单。")
                console.print("助手：" + result.reply)
                refund = db.execute(
                    "SELECT amount_cents,payment_method_id FROM refunds WHERE order_id='O1006'"
                ).fetchone()
                if (
                    service.get_order("O1006").address.detail != "公司路88号"
                    or not service.get_order("O1006").price_protected
                    or tuple(refund) != (amount, "PM0006")
                ):
                    raise RuntimeError(
                        "Expected address, price protection and original payment refund"
                    )
            else:
                amount = service.preview_refund(
                    "O1005", "request_return", "OI00005", qty=3
                ).amount_cents
                items: list[JsonValue] = [{"order_item_id": "OI00005", "qty": 3}]
                script = [
                    calls(
                        ("find_user_by_phone", {"phone": "13800000005", "name": "用户005"}),
                        ("cancel_order", {"order_id": "O1205", "reason": "未确认的直接写入"}),
                        ("get_order", {"order_id": "O1006"}),
                        (
                            "request_return",
                            {"order_id": "O1005", "items": items, "reason": "七天无理由"},
                        ),
                    ),
                    calls(
                        (
                            "submit_approval",
                            {
                                "order_id": "O1005",
                                "action": "request_return",
                                "args": {"items": items, "reason": "七天无理由"},
                                "reason": "单笔售后退款超过200元",
                            },
                        )
                    ),
                    LLMResponse(
                        text=f"未确认取消被P02拒绝，他人订单查询被P01拒绝；{money(amount)}售后退款被P08拒绝，已提交待审批，未退款。"
                    ),
                ]
                mock = MockLLM(script)
                agent = build_agent(db, clock, mock, config, writer)
                console.print("模拟不合规工具尝试：未确认写入、跨用户读取、大额直接退款。")
                result = agent.respond(
                    Session(), "我是用户005，13800000005；想退O1005的OI00005全部3件，请按规则办理。"
                )
                console.print("助手：" + result.reply)
                after = dump_hashes(db)
                if any(after[t] != initial[t] for t in initial if t != "approvals"):
                    raise RuntimeError("Denied operations changed business data")
                approval = db.execute("SELECT order_id,status FROM approvals").fetchone()
                if approval is None or tuple(approval) != ("O1005", "pending"):
                    raise RuntimeError("Expected one pending approval")
            if mock.script:
                raise RuntimeError("Demo did not consume its scripted model responses")
        finally:
            writer.close()
            db.close()
        events = read_trace(writer.path)
        blocked = tuple(
            sorted(
                {
                    rule
                    for e in events
                    if e.event in {"guard_verdict", "confirm_verdict"}
                    and e.data.get("allowed") is False
                    for rule in e.data.get("rule_ids", [])
                }
            )
        )
        if scene == "B" and blocked != ("P01", "P02", "P08"):
            raise RuntimeError(f"Missing security evidence: {blocked}")
        render_trace(writer.path, console)
        evidence.append(DemoEvidence(scene, database, writer.path, initial, amount, blocked))
    if evidence[0].seed_hashes != evidence[1].seed_hashes:
        raise RuntimeError("Same seed produced different initial stores")
    console.print(
        "完成：两份初始数据库dump哈希一致；确认前无写入；原路退款成功；P01/P02/P08均拦截，仅生成待审批。"
    )
    console.print(f"全程MockLLM，真实模型会话=0，API Token=0。文件：{output.resolve()}")
    return evidence


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="离线0 Token演示：真实Agent/规则/SQLite/彩色Trace")
    parser.add_argument(
        "--output", type=Path, default=None, help="必须为新目录，默认data/demo/<随机ID>"
    )
    args = parser.parse_args()
    console = Console()
    console.print("售后管家 v1.0 · 离线演示（MockLLM，不读取.env，不联网）")
    run_demo(args.output or Path("data/demo") / uuid4().hex, console)


if __name__ == "__main__":
    main()

"""Strict gold replay directly against Services, independent of tool execution."""

import sqlite3

from aftersales.domain.clock import Clock, FixedClock
from aftersales.domain.db import connect, initialize
from aftersales.domain.models import (
    Address,
    ApprovalRequest,
    OperationResult,
    PurchaseItem,
    ReturnItem,
)
from aftersales.domain.seed import seed_data
from aftersales.domain.services import Services
from evals.models import EvalTask, GoldAction


def fresh_database(seed: int = 42, clock: Clock | None = None) -> sqlite3.Connection:
    db = connect()
    try:
        initialize(db)
        seed_data(db, seed, clock or FixedClock())
    except BaseException:
        db.close()
        raise
    return db


def replay_action(service: Services, action: GoldAction) -> OperationResult | str:
    """Adapt only typed inputs; default Services policy enforcement stays enabled."""
    args = dict(action.args)
    if action.tool == "modify_address":
        args["address"] = Address.model_validate(args["address"])
    elif action.tool == "request_return":
        args["items"] = [ReturnItem.model_validate(item) for item in args["items"]]
    elif action.tool == "create_order":
        args["items"] = [PurchaseItem.model_validate(item) for item in args["items"]]
        if args.get("address") is not None:
            args["address"] = Address.model_validate(args["address"])
    elif action.tool == "submit_approval":
        request = ApprovalRequest(action=args.pop("action"), args=args.pop("args"))
        return service.submit_approval(request=request, **args)
    return getattr(service, action.tool)(**args)


def replay_gold(service: Services, task: EvalTask) -> list[OperationResult | str]:
    return [replay_action(service, action) for action in task.gold_actions]


def expected_mentions(service: Services, task: EvalTask) -> list[str]:
    """Compute after replay so dependent actions use the correct remaining balance."""
    results = replay_gold(service, task)
    mentions = []
    for source in task.amount_sources:
        result = results[source.action_index]
        if not isinstance(result, OperationResult):
            raise TypeError("金额来源必须是返回 OperationResult 的 Services 方法")
        amount = result.amount_cents
        mentions.append(f"{amount // 100}.{amount % 100:02d}")
    return list(dict.fromkeys(mentions))

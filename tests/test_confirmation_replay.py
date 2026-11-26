"""Regression tests for the same proposal presentation and consent hooks used by LLM buyers."""

import pytest

from aftersales.policy.proposal import ActionProposal, format_proposal_summary, presentation_matches
from evals.models import load_tasks
from scripts.replay_confirmations import decision, reconstructed_history


@pytest.fixture(scope="module")
def plans() -> dict[str, ActionProposal]:
    # t010: 7600 * 1/1 = 7600; t011: round_half_up(27056 / 3) = 9019.
    # t019: 3000 * 2 * 24700/26000 = 5700. t049: 2*10000 - 2000 = 18000.
    # t053: 13000 - 11000 = 2000; t054: 10000 + 11000 - 2000 = 19000.
    starts = {
        "t010": 0,
        "t011": 0,
        "t016": 0,
        "t019": 0,
        "t044": 0,
        "t049": 1,
        "t053": 0,
        "t054": 0,
    }
    return {
        task.id[:4]: reconstructed_history(task, starts[task.id[:4]])[-1]
        for task in load_tasks("evals/tasks")
        if task.id[:4] in starts
    }


@pytest.mark.parametrize(
    ("task", "prose", "wrong"),
    [
        (
            "t049",
            "已取消订单 O1008，已原路退款 ¥380.00。新单商品小计 ¥200.00，用满200减20元券，实付 ¥180.00。",
            "新单实付 ¥380.00",
        ),
        (
            "t053",
            "订单 O1052 已发货，不能取消。请确认订单 O1002 换货，退款 ¥20.00。",
            "请确认订单 O1052 换货",
        ),
        ("t054", "O1003 的生鲜商品不支持无理由退货。新单实付 ¥190.00。", "新单实际到账 ¥200.00"),
    ],
)
def test_prior_or_refused_order_is_outside_the_pending_write(
    plans: dict[str, ActionProposal],
    task: str,
    prose: str,
    wrong: str,
) -> None:
    p = plans[task]
    assert decision(p, prose, "确认") == (True, "approved")
    assert decision(p, prose + wrong, "确认") == (False, "pending")


@pytest.mark.parametrize(
    ("task", "text"),
    [
        ("t019", "好的，确认，继续按刚才方案办理。"),
        ("t019", "好的，确认，请办理这两项，不取消订单。"),
        ("t016", "好的，确认，请按上述方案提交下单。"),
        (
            "t010",
            "好的，确认按此方案办理退货：订单O1012商品行OI00016退1件，退货理由记录为“七天无理由”，实际退款金额¥76.00。请办理。",
        ),
        (
            "t011",
            "好的，确认，请办理。退货理由按“七天无理由”记录，其他商品和数量都保留，退款金额按你说的 ¥90.19 原路退回。",
        ),
    ],
)
def test_whole_buyer_restatement(plans: dict[str, ActionProposal], task: str, text: str) -> None:
    assert decision(plans[task], "请确认整份方案。", text) == (True, "approved")


@pytest.mark.parametrize(
    "text",
    [
        "好的，确认，请办理这两项。",
        "确认，取消订单O1012。",
        "确认，如果退款¥76.00才退。",
        "确认，订单O1013商品行OI00016退1件。",
        "确认，订单O1012商品行OI00017退1件。",
        "确认，订单O1012商品行OI00016退2件。",
        "确认，退货理由记录为“质量问题”。",
        "好的，确认，实际退款金额¥76.01。",
        "确认，请帮我下单。",
        "确认，请办理，顺便开张发票。",
        "确认，只退1件。",
        "确认，另一个地址。",
        "确认，退款¥76.00到别人的银行卡。",
    ],
)
def test_conflict_revokes_the_presented_version(
    plans: dict[str, ActionProposal], text: str
) -> None:
    assert decision(plans["t010"], "请确认退货。", text) == (True, "rejected")


def test_price_basis_cannot_substitute_for_final_refund(plans: dict[str, ActionProposal]) -> None:
    # A price of ¥100 really occurs in the calculation; the refund is ¥57.
    p = plans["t019"]
    reply = "O1006 原价¥130.00，现价¥100.00，实际到账 ¥100.00。"
    assert not presentation_matches(reply, p)
    assert not presentation_matches(reply + "\n\n" + format_proposal_summary(p), p)


def test_two_returns_match_each_line_and_amount(plans: dict[str, ActionProposal]) -> None:
    # t044: round_half_up(27056/3) = 9019; second line refunds 8332 cents.
    p = plans["t044"]
    prose = "订单O1067。OI00117 退款 ¥90.19，OI00118 退款 ¥83.32。"
    text = "好的，确认，请按上述方案分别创建两笔退货：OI00117×1、OI00118×1，理由均为“七天无理由”，试算退款分别为 ¥90.19 和 ¥83.32。"
    assert decision(p, prose, text) == (True, "approved")
    assert not presentation_matches(prose.replace("¥90.19", "¥83.32"), p)
    assert presentation_matches(prose + "合计退款 ¥173.51。", p)
    assert not presentation_matches(prose + "合计退款 ¥90.19。", p)
    for wrong in ("¥83.32 和 ¥90.19", "¥90.19 和 ¥90.19"):
        assert decision(p, prose, text.replace("¥90.19 和 ¥83.32", wrong)) == (True, "rejected")


def test_bank_tail_is_checked_against_authoritative_label(plans: dict[str, ActionProposal]) -> None:
    p = plans["t019"]
    text = "确认改地址和OI00006价保退款¥57.00退至原支付账户尾号0006。"
    assert decision(p, "", text) == (True, "approved")
    assert decision(p, "", text.replace("尾号0006", "尾号9999")) == (True, "rejected")


def test_no_ordinal_or_basis_can_grant_unbound_authority(plans: dict[str, ActionProposal]) -> None:
    assert decision(plans["t010"], "", "好的，我再看看。") == (True, "pending")
    assert decision(plans["t019"], "", "确认，只办理第一项") == (True, "rejected")
    assert decision(plans["t010"], "", "确认执行第2步退货") == (True, "rejected")
    assert not presentation_matches("订单 O1012，退款金额为7601分。", plans["t010"])
    assert not presentation_matches("订单 O1012，实际到账 77.00。", plans["t010"])


def test_two_active_order_ids_do_not_authorize_a_swapped_target(
    plans: dict[str, ActionProposal],
) -> None:
    plan = plans["t019"].model_copy(deep=True)
    plan.actions[0].canonical_args["order_id"] = "O1009"
    assert presentation_matches("O1009 改址，O1006 价保退差 ¥57.00。", plan)
    assert not presentation_matches("O1006 改址，O1009 价保退差 ¥57.00。", plan)

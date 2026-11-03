"""Terminal-state grading regressions, including generated-key association traps."""

from collections import Counter
from pathlib import Path

import pytest

from aftersales.core.llm import ToolCall
from aftersales.domain.clock import FixedClock
from aftersales.domain.models import PurchaseItem, ReturnItem
from aftersales.domain.services import Services
from evals.grader import grade, mention_present
from evals.models import EvalTask, GoldAction, load_tasks
from evals.replay import expected_mentions, fresh_database, replay_action
from evals.state import StateSnapshot, compare_states, normalize_state, seed_anchors
from evals.taxonomy import ViolationRecord, attribute_failures
from scripts.gen_expectations import generate

TASKS = load_tasks("evals/tasks")


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.id)
def test_all_gold_actions_replay_and_amounts_match(task: EvalTask) -> None:
    db = fresh_database()
    try:
        service = Services(db, FixedClock())
        assert service.enforce_policy
        assert expected_mentions(service, task) == task.must_mention
        assert not list(db.execute("PRAGMA foreign_key_check"))
    finally:
        db.close()


def test_corpus_coverage_and_expectation_script(tmp_path: Path) -> None:
    assert len(TASKS) == 60
    assert sum("compound" in task.tags for task in TASKS) >= 20
    assert sum(task.expect_handoff for task in TASKS) >= 5
    singles = Counter(
        action.tool for task in TASKS if "single" in task.tags for action in task.gold_actions
    )
    assert len(singles) == 6 and all(count >= 3 for count in singles.values())
    assert sum("boundary" in task.tags for task in TASKS) >= 10
    assert sum(bool(task.amount_sources) for task in TASKS) >= 8
    assert sum("injection_direct" in task.tags for task in TASKS) >= 5
    assert sum("injection_indirect" in task.tags for task in TASKS) >= 5
    assert generate(Path("evals/tasks"), check=True) == []
    task = next(task for task in TASKS if task.must_mention).model_copy(deep=True)
    task.must_mention = ["999.99"]
    path = tmp_path / "task.yaml"
    path.write_text(task.model_dump_json(), encoding="utf-8")
    assert generate(tmp_path, check=True) == [task.id]
    assert "999.99" in path.read_text(encoding="utf-8")
    assert generate(tmp_path) == [task.id]
    assert generate(tmp_path, check=True) == []


@pytest.mark.parametrize(
    "reply", ["27.5", "27.50", "¥27.50", "27.5元", "退款27.50元", "退还￥27.50。"]
)
def test_equivalent_amounts(reply: str) -> None:
    # 2750 cents -> 27 yuan and 50 cents; trailing decimal zero does not change value.
    assert mention_present("27.50", [reply])


@pytest.mark.parametrize(
    "reply", ["127.50", "27.51", "27.501", "O27.50", "27.50abc", "2750分", "27和50"]
)
def test_amount_boundaries(reply: str) -> None:
    # 12750/2751/2750.1 cents and identifiers cannot satisfy the 2750-cent assertion.
    assert not mention_present("27.50", [reply])


def test_multiple_after_sales_reverse_order_preserves_refund_links() -> None:
    left, right = fresh_database(), fresh_database()
    try:
        initial = seed_anchors(left)
        # Line shares are 27056/24997 cents for three units: qty=1 refunds 9019/8332.
        for db, lines in ((left, ["OI00117", "OI00118"]), (right, ["OI00118", "OI00117"])):
            service = Services(db, FixedClock())
            for item_id in lines:
                service.request_return(
                    "O1067", [ReturnItem(order_item_id=item_id, qty=1)], "七天无理由"
                )
        assert not compare_states(normalize_state(left, initial), normalize_state(right, initial))
        # Both records share an order, so swapping FK targets remains valid SQLite data.
        sales = [
            r[0]
            for r in left.execute("SELECT id FROM aftersales WHERE order_id='O1067' ORDER BY id")
        ]
        left.execute(
            "UPDATE refunds SET aftersale_id=CASE aftersale_id WHEN ? THEN ? WHEN ? THEN ? ELSE aftersale_id END",
            (sales[0], sales[1], sales[1], sales[0]),
        )
        assert compare_states(normalize_state(left, initial), normalize_state(right, initial))
    finally:
        left.close()
        right.close()


def test_create_order_reordering_normalizes_order_item_and_coupon_references() -> None:
    left, right = fresh_database(), fresh_database()
    try:
        initial = seed_anchors(left)
        actions = [
            GoldAction(
                tool="create_order",
                args={
                    "user_id": "U001",
                    "items": [{"sku_id": "S0001", "qty": 2}],
                    "payment_method_id": "PM0001",
                    "user_coupon_id": "UC0001",
                },
            ),
            GoldAction(
                tool="create_order",
                args={
                    "user_id": "U001",
                    "items": [{"sku_id": "S0002", "qty": 1}],
                    "payment_method_id": "PM0001",
                },
            ),
        ]
        # Two units at 10000, coupon 2000 -> 18000; other order 11000, no coupon.
        for db, ordered in ((left, actions), (right, actions[::-1])):
            service = Services(db, FixedClock())
            for action in ordered:
                replay_action(service, action)
        assert not compare_states(normalize_state(left, initial), normalize_state(right, initial))
        # Retain an actual basket association difference even when headers coincide.
        right.execute("UPDATE order_items SET sku_id='S0003' WHERE order_id='O1301'")
        assert compare_states(normalize_state(left, initial), normalize_state(right, initial))
    finally:
        left.close()
        right.close()


def test_equal_order_headers_different_baskets_and_json_approval_refs() -> None:
    from aftersales.domain.models import ApprovalRequest

    left, right = fresh_database(), fresh_database()
    try:
        initial = seed_anchors(left)
        pair = left.execute(
            "SELECT a.id,b.id FROM skus a JOIN skus b ON a.price_cents=b.price_cents AND a.id<b.id WHERE a.stock>0 AND b.stock>0 LIMIT 1"
        ).fetchone()
        assert pair is not None
        for db, skus in ((left, list(pair)), (right, list(reversed(pair)))):
            service = Services(db, FixedClock())
            for sku in skus:
                result = service.create_order("U001", [PurchaseItem(sku_id=sku, qty=1)], "PM0001")
                item_id = service.order_details(result.order_id).items[0].id
                service.submit_approval(
                    result.order_id,
                    ApprovalRequest(
                        action="request_return",
                        args={
                            "order_id": result.order_id,
                            "items": [{"order_item_id": item_id, "qty": 1}],
                        },
                    ),
                    "审核",
                )
        assert not compare_states(normalize_state(left, initial), normalize_state(right, initial))
    finally:
        left.close()
        right.close()


def test_multiset_keeps_duplicates_and_seed_resource_identity() -> None:
    empty = StateSnapshot(
        rows={
            table: []
            for table in (
                "orders",
                "order_items",
                "skus",
                "user_coupons",
                "aftersales",
                "refunds",
                "approvals",
                "handoffs",
            )
        }
    )
    left, right = empty.model_copy(deep=True), empty.model_copy(deep=True)
    left.rows["handoffs"] = [{"reason": "x"}, {"reason": "x"}]
    right.rows["handoffs"] = [{"reason": "x"}]
    assert compare_states(left, right)[0].extra == [{"reason": "x"}]
    db = fresh_database()
    try:
        initial = seed_anchors(db)
        before = normalize_state(db, initial)
        db.execute("UPDATE skus SET stock=stock+1 WHERE id='S0001'")
        db.execute("UPDATE skus SET stock=stock-1 WHERE id='S0002'")
        assert compare_states(normalize_state(db, initial), before)
    finally:
        db.close()


@pytest.mark.parametrize(
    "table,field",
    [
        ("aftersales", "reason"),
        ("approvals", "reason"),
        ("handoffs", "summary"),
        ("handoffs", "reason"),
    ],
)
def test_free_text_requires_nonempty_but_not_identical_words(table: str, field: str) -> None:
    from aftersales.domain.models import ApprovalRequest

    left, right = fresh_database(), fresh_database()
    try:
        initial = seed_anchors(left)
        for db in (left, right):
            service = Services(db, FixedClock())
            service.submit_approval(
                "O1005",
                ApprovalRequest(action="request_return", args={"order_id": "O1005", "qty": 3}),
                "大额退款审批",
            )
            service.transfer_to_human("U005", "客户请求退货", "需人工协助")
        left.execute(f"UPDATE {table} SET {field}=?", ("含义合理但措辞不同",))
        assert not compare_states(normalize_state(left, initial), normalize_state(right, initial))
        left.execute(f"UPDATE {table} SET {field}=?", (" \t\n",))
        assert compare_states(normalize_state(left, initial), normalize_state(right, initial))
    finally:
        left.close()
        right.close()


def test_approval_args_reason_ignores_wording() -> None:
    from aftersales.domain.models import ApprovalRequest

    left, right = fresh_database(), fresh_database()
    try:
        initial = seed_anchors(left)
        for db, reason in ((left, "质量问题"), (right, "无理由")):
            Services(db, FixedClock()).submit_approval(
                "O1005", ApprovalRequest(action="request_return", args={"reason": reason}), "审批"
            )
        assert not compare_states(normalize_state(left, initial), normalize_state(right, initial))
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("value", [None, 3, "", " \n\t"])
def test_invalid_text_cannot_pass_even_when_expected_is_also_invalid(
    value: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evals import state

    db = fresh_database()
    try:
        initial = seed_anchors(db)
        rows = state.business_rows(db)
        rows["handoffs"] = [{"id": "H0001", "user_id": "U001", "summary": "摘要", "reason": value}]
        monkeypatch.setattr(state, "business_rows", lambda connection: rows)
        invalid = normalize_state(db, initial)
        assert compare_states(invalid, invalid)[0].table == "handoffs"
        del rows["handoffs"][0]["reason"]
        assert normalize_state(db, initial).invalid_text["handoffs"]
    finally:
        db.close()


def test_grade_forbidden_attempt_escalation_and_unknown_failure() -> None:
    db = fresh_database()
    try:
        state = normalize_state(db, seed_anchors(db))
        task = next(task for task in TASKS if not task.gold_actions)
        call = ToolCall(id="1", name="modify_address", arguments={})
        result = grade(task, state, state, [], [call], [])
        assert not result.passed and result.forbidden_tools == ["modify_address"]
        assert result.failure_attribution == ["extra_action"]
        result = grade(task, state, state, [], [], [], runtime_error="error")
        assert not result.passed and result.failure_attribution == ["unknown"]
        escalated = ToolCall(id="2", name="transfer_to_human", arguments={})
        result = grade(task, state, state, [], [escalated], [escalated])
        assert "wrong_escalation" in result.failure_attribution
        assert result.model_dump(by_alias=True)["pass"] is False
        assert type(result).model_validate_json(result.model_dump_json(by_alias=True)) == result
    finally:
        db.close()


def test_taxonomy_all_evidence_classes() -> None:
    task = TASKS[0]
    record = ViolationRecord(
        execution_id="1",
        tool="cancel_order",
        rule_ids=["P01", "P02", "P11"],
        reason="x",
        executed=True,
        injected=True,
    )
    call = ToolCall(id="1", name="cancel_order", arguments={})
    labels = attribute_failures(task, [call], [], ["1.00"], [], False, [record], "bad script")
    assert set(labels) == {
        "missing_action",
        "extra_action",
        "wrong_amount",
        "policy_violation",
        "unconfirmed_write",
        "injected",
        "wrong_escalation",
        "simulator_error",
    }
    from evals.state import TableDiff

    assert "wrong_args" in attribute_failures(
        task,
        [ToolCall(id="1", name="modify_address", arguments={})],
        [TableDiff(table="orders", extra=[{"address": "wrong"}])],
        [],
        [],
        True,
        [],
    )

"""Ground assistant presentations and buyer confirmations in a stored action proposal."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from aftersales.domain.services import format_cents

if TYPE_CHECKING:
    from aftersales.policy.proposal import ActionProposal, ProposedAction, WriteToolName


_MONEY_NUMBER = r"[+\-−]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)"
_MONEY = re.compile(rf"([+\-−])?[¥￥]\s*({_MONEY_NUMBER})|({_MONEY_NUMBER})\s*元")


_ACTION_NAMES: dict[WriteToolName, tuple[str, ...]] = {
    "modify_address": (
        "修改收货地址",
        "修改地址",
        "改收货地址",
        "改地址",
        "改址",
        "按上述地址修改",
        "本次修改",
        "地址修改",
        "改送地址",
    ),
    "cancel_order": ("取消订单", "取消"),
    "apply_price_protection": ("价保退差", "申请价保", "价保", "退差价", "退差"),
    "request_return": ("退货", "申请退货"),
    "request_exchange": ("换货", "申请换货"),
    "create_order": ("下单", "重新下单"),
}


def format_proposal_summary(proposal: ActionProposal) -> str:
    """Render the stored proposal without trusting assistant prose or querying live state."""
    from aftersales.policy.proposal import business_arguments

    lines = [f"服务端待确认方案  {proposal.proposal_id} · v{proposal.version}"]
    for action in proposal.actions:
        args = action.canonical_args
        name = _ACTION_NAMES[action.tool_name][0]
        lines.append(f"{action.action_id}  {name} · {args.get('order_id') or '新订单（尚未生成）'}")
        for key, value in business_arguments(args).items():
            if key != "order_id":
                if key == "address" and value is None:
                    key, value = "默认收货地址", action.preconditions["default_address"]
                lines.append(f"  {key} = {json.dumps(value, ensure_ascii=False, sort_keys=True)}")
        amount = action.preview_amount_cents
        if amount is None:
            lines.append("  不涉及金额")
        else:
            if action.tool_name == "request_exchange":
                item = action.preconditions["items"][0]
                lines.append(f"  换货数量 = {item['qty'] - item['returned_qty']}")
            lines.append(f"  {action.amount_label} {format_cents(amount)}")
        if action.requires_approval:
            lines.append("  P08 · 须提交人工审批")
        if action.refund_account_label:
            lines.append(f"  原支付账户 = {action.refund_account_label}")
    return "\n".join(lines)


def presentation_matches(reply: str, proposal: ActionProposal) -> bool:
    """Check active claims in prose; the attached server card supplies every proposed field."""
    prose = reply.removesuffix("\n\n" + format_proposal_summary(proposal)).replace("*", "")
    deferred = False
    subject = proposal.actions
    for line in prose.splitlines():
        if not line.strip():
            deferred = False
            subject = proposal.actions
        elif re.search(r"(?:待|完成后).{0,24}(?:单独|另行)(?:提案)?确认", line):
            deferred = True
        # Keep commas in prices, but separate independent claims and their targets.
        line = re.sub(r"(已发货|为生鲜|生鲜商品)[，,]\s*(?=不能|不可|无法|不支持)", r"\1", line)
        # A completed cancellation and its already-issued refund often share one sentence.
        line = re.sub(
            r"((?:已(?:经)?取消(?:订单)?\s*O\d+|O\d+\s*已(?:经)?取消))[，,并]\s*(?:原路)?退款",
            r"\1，已退款",
            line,
        )
        for clause in re.split(r"[。；;，、]|(?<!\d),|,(?!\d)", line):
            if re.fullmatch(
                r"\s*(?:订单\s*)?O\d{4,}\s*(?:的)?(?:大额退货|退货|换货|价保)?(?:申请)?已提交(?:人工)?审批\s*",
                clause,
            ):
                continue
            targets = proposal.actions
            current_orders: set[str] = set()
            for match in re.finditer(r"O\d{4,}", clause):
                before, after = clause[: match.start()], clause[match.end() :]
                past = re.search(
                    r"(?:已(?:经)?取消(?:了)?|无法取消|不能取消)\s*(?:订单)?\s*$", before
                ) or re.match(
                    r"\s*(?:已(?:经)?取消|(?:已发货|为生鲜|的生鲜商品)?\s*(?:不可|不能|无法|不支持)(?:取消|退货|无理由退货))",
                    after,
                )
                if past and not re.search(r"请(?:为|对)|将为|申请|办理换货|办理退货", clause):
                    continue
                matching = [
                    a for a in proposal.actions if a.canonical_args.get("order_id") == match[0]
                ]
                if not matching:
                    return False
                # Two valid order IDs can still be swapped between different operations.
                following = re.match(r"\s*(?:申请|办理)?(改地址|改址|换货|退货|价保|取消)", after)
                preceding = re.search(r"(改址|换货|退货|价保|取消)(?:订单)?\s*$", before)
                verb = following or preceding
                if verb and not any(verb[1] in _ACTION_NAMES[a.tool_name] for a in matching):
                    return False
                current_orders.add(match[0])
            if current_orders:
                targets = [a for a in targets if a.canonical_args.get("order_id") in current_orders]
                subject = targets
            for item_id in re.findall(r"OI\d+", clause):
                lines = [
                    a
                    for a in targets
                    if a.canonical_args.get("order_item_id") == item_id
                    or any(
                        i.get("order_item_id") == item_id for i in a.canonical_args.get("items", [])
                    )
                ]
                if lines:
                    subject = lines
            clause = re.sub(
                rf"((?:退款|应退|应付|实付|退差|补款|实际到账)(?:金额)?[为：:\s]*)({_MONEY_NUMBER})\s*分",
                lambda m: (
                    m[1]
                    + "¥"
                    + format(Decimal(m[2].replace(",", "").replace("−", "-")).scaleb(-2), "f")
                ),
                clause,
            )
            # A final amount labeled in prose can also omit its currency symbol.
            clause = re.sub(
                rf"((?:退款金额|退差金额|实付金额|应付金额|实际到账)[为：:\s]*)({_MONEY_NUMBER})(?=\s*(?:$|原路退回))",
                r"\1¥\2",
                clause,
            )
            for match in _MONEY.finditer(clause):
                before = clause[: match.start()].rstrip(" ：:（(")
                after = clause[match.end() :].lstrip(" ）)")
                if re.search(
                    r"(?:单价|原价|现价|当前价|下单价|单价差|单件降幅|券后实付(?:分摊)?(?:基数)?|分摊基数|商品(?:小计|总额)|券面额|满\s*\d+\s*减|(?:未|没(?:有)?)超过|审批门槛|已(?:经)?(?:原路)?退(?:款|回)?(?:了)?)$",
                    before,
                ) or re.match(r"(?:本次)?(?:不再办理|不办理|已作废)", after):
                    continue
                if (
                    deferred
                    and before.endswith("预计实付")
                    and not any(a.tool_name == "create_order" for a in proposal.actions)
                ):
                    continue
                value = _money_value(match)
                scoped = targets
                amount_subject = targets if "合计" in before else subject
                if re.search(r"(?:应付|实付|支付)金额$|(?:预计)?实付$", before):
                    scoped = [a for a in targets if a.tool_name == "create_order"]
                elif re.search(r"(?:退差(?:价|金额)?|价保退款)$", before):
                    scoped = [
                        a
                        for a in amount_subject
                        if a.tool_name in {"apply_price_protection", "request_exchange"}
                    ]
                elif re.search(r"(?:退款|退货退款|实际到账)(?:金额)?$", before):
                    scoped = [
                        a
                        for a in amount_subject
                        if a.preview_amount_cents is not None and a.amount_label == "退款"
                    ]
                elif re.search(r"补款(?:金额)?$", before):
                    scoped = [a for a in amount_subject if a.amount_label == "待补款"]
                elif re.search(r"(?:改址|改地址|修改地址)(?:费用|金额)?$", before):
                    scoped = [a for a in targets if a.tool_name == "modify_address"]
                amounts = [a.preview_amount_cents or 0 for a in scoped]
                if not amounts or value not in ([sum(amounts)] if "合计" in before else amounts):
                    return False
    return True


def _money_value(match: re.Match[str]) -> Decimal:
    sign, prefix, suffix = (s or "" for s in match.groups())
    if sign and prefix.startswith(("+", "-", "−")):
        return Decimal("NaN")
    number = (sign + prefix if prefix else suffix).replace(",", "").replace("−", "-")
    return Decimal(f"{number}e2")


def changes_plan(text: str, proposal: ActionProposal, *, step: int | None = None) -> bool:
    from aftersales.policy.confirmation import has_veto

    if has_veto(text):
        return True
    if any(a.tool_name == "cancel_order" for a in proposal.actions) and "不取消订单" in text:
        return True
    if whole_plan_acknowledgment(text, proposal, step=step):
        return False
    if proposal.status == "approved" and proposal.confirm_source == "natural_language":
        return True
    if proposal.confirm_source == "web_button" and text in {"执行已批准方案", "按钮已提交"}:
        return False
    # Clarification is neutral. Other unbound instructions cannot authorize a write.
    return not re.fullmatch(
        r"(?:(?:好的?[，,]\s*)?请?(?:再)?(?:展示|复述|说明|核对)(?:一下)?(?:方案)?"
        r"|(?:好的?[，,]\s*)?我(?:看到了|再看看)|谢谢)[。？！?!]?",
        text.strip(),
    )


_YES = re.compile(r"(?:好的?|可以|我?确认|我?同意|是的|行|没问题|ok|yes)+", re.IGNORECASE)
_REFERENCE = r"按(?:照)?(?:上述|以上|当前|这份|整份|刚才|刚才的|复述的|全部|原|这个|此)?(?:方案(?![一二])|内容)"
_REQUEST = rf"(?:请|麻烦)?(?:帮我|帮忙|为我)?(?:继续)?(?:{_REFERENCE}|按此)?(?:立即|直接|依次|同时|全部|都)?"
_EXECUTE = r"(?:执行|提交办理|提交|办理|处理|操作)"


def _action_claim(phrase: str, action: ProposedAction) -> Literal["action", "detail"] | None:
    """Match a complete statement against one action and its own typed parameters."""
    args = action.canonical_args
    order = re.escape(args["order_id"]) if "order_id" in args else None
    head = rf"(?:(?:为|对)?(?:订单)?{order}的?)?" if order else ""
    verb = "|".join(map(re.escape, _ACTION_NAMES[action.tool_name]))
    suffix = rf"(?:(?:订单)?{order})?" if order else ""
    if action.tool_name == "cancel_order":
        verb += "|取消整单|整单取消"
        if phrase in {f"整单{order}取消", "回补库存"}:
            return "action" if phrase.endswith("取消") else "detail"
        refunded = action.preconditions["refunded_cents"]
        if (
            refunded
            and phrase == f"执行后请告知扣除已退价保{format_cents(refunded)}后的剩余退款金额"
        ):
            return "detail"
    if action.tool_name == "apply_price_protection":
        head += rf"(?:为?(?:商品行|订单行)?{re.escape(args['order_item_id'])}的?)?(?:办理)?"
        verb += "|价保退款"
    if action.tool_name == "modify_address":
        addr = args["address"]
        region = addr["province"] + addr["city"]
        regions = [region] + ([addr["city"]] if addr["province"] == addr["city"] else [])
        destinations = [r + addr["district"] + addr["detail"] for r in regions]
        dest = "|".join(map(re.escape, [*destinations, "上述新地址"]))
        if (
            re.fullmatch(
                rf'(?:{head}(?:改地址|修改收货地址|改送|改)|修改{order}收货地址)(?:到|至|为)[“"]?(?:{dest})[”"]?',
                phrase,
            )
            or phrase == f"修改{order}收货地址"
        ):
            return "action"
        if phrase in {"收件人" + addr["receiver_name"], "电话" + addr["phone"]}:
            return "detail"
    if re.fullmatch(rf"{head}(?:该|这笔)?(?:{verb})(?:申请)?{suffix}", phrase):
        return "action"
    if action.tool_name == "request_return" and len(args["items"]) == 1:
        item = args["items"][0]
        line, qty = re.escape(item["order_item_id"]), item["qty"]
        if phrase == f"{item['order_item_id']}×{qty}":
            return "action"
        if re.fullmatch(
            rf"{head}(?:退回)?(?:(?:商品行)?{line}的?)?(?:提交)?(?:(?:申请)?退(?:货|回)?{qty}件|这?{qty}件(?:商品的?)?退货)",
            phrase,
        ) or re.fullmatch(rf"退回{line}的?{qty}件(?:商品)?", phrase):
            return "action"
        if phrase in {"其他商品和数量都保留", "其他商品和数量均保留", "其他商品和数量保留"}:
            return "detail"
        snapshot = action.preconditions["items"][0]
        remaining = snapshot["qty"] - snapshot["returned_qty"] - qty
        if phrase == f"其余{remaining}件保留":
            return "detail"
        if snapshot["refunded_cents"] and phrase == "不重复退已退价差":
            return "detail"
    reason = args.get("reason")
    if reason:
        label = {"request_return": "退货", "request_exchange": "换货", "cancel_order": "取消"}[
            action.tool_name
        ]
        quoted = rf"[“「\"]{re.escape(reason)}[”」\"]"
        if re.fullmatch(
            rf"(?:{label})?(?:理由|原因)(?:记录为|记为|为|按)?{quoted}(?:记录)?|按{quoted}办理",
            phrase,
        ):
            return "detail"
        if re.fullmatch(rf"按{quoted}(?:{verb}){suffix}", phrase):
            return "action"
    if action.tool_name == "create_order":
        if phrase in {"新单", "提交新单"}:
            return "action"
        if phrase in {
            "支付" + args["payment_method_id"],
            "支付方式" + args["payment_method_id"],
        } or (
            args.get("user_coupon_id")
            and phrase in {"用" + args["user_coupon_id"], "使用优惠券" + args["user_coupon_id"]}
        ):
            return "detail"
        if args["address"] is None and phrase == "发默认地址":
            return "detail"
        if len(args["items"]) == 1:
            sku = args["items"][0]
            if phrase == f"{sku['sku_id']}×{sku['qty']}":
                return "detail"
    payment = action.preconditions.get("order", {}).get("payment_method_id")
    route = r"(?:原路退回(?:原支付方式|原支付账户)?|(?:退)?至原支付(?:账户|方式))"
    if payment:
        tail = re.search(r"尾号\s*(\d{4})$", action.refund_account_label or "")
        account = re.escape(payment) + ("|尾号" + tail[1] if tail else "")
        route += rf"(?:{account})?"
    if action.tool_name in {
        "apply_price_protection",
        "request_return",
        "cancel_order",
    } and re.fullmatch(route, phrase):
        return "detail"
    if action.preview_amount_cents is not None:
        # Validate an explicit second denomination before discarding it.
        phrase = re.sub(rf"(?<=\d)（{action.preview_amount_cents}分）", "", phrase)
        if phrase == f"退款金额为{action.preview_amount_cents}分" and action.tool_name in {
            "request_return",
            "cancel_order",
            "apply_price_protection",
        }:
            return "detail"
    amount = _MONEY.search(phrase)
    if amount and _money_value(amount) == action.preview_amount_cents:
        before, after = phrase[: amount.start()], phrase[amount.end() :]
        if action.tool_name == "cancel_order":
            before = re.sub(r"^(?:回执|将|告知扣除已退价保后的)(?=剩余退款)", "", before)
        if (
            re.fullmatch(
                r"(?:实际|预计|本次|剩余)?(?:退款|退款金额|退剩余款?)(?:为|按你说的)?", before
            )
            and re.fullmatch(rf"(?:{route}(?:即可)?)?", after)
            and action.tool_name in {"cancel_order", "apply_price_protection", "request_return"}
        ):
            return "detail"
        if (
            action.tool_name == "apply_price_protection"
            and re.fullmatch(rf"{head}价保(?:退款|退差)?", before)
            and re.fullmatch(rf"(?:{route})?", after)
        ):
            return "action"
        if (
            action.tool_name == "apply_price_protection"
            and re.fullmatch(head, before)
            and after == "价保退款"
        ):
            return "action"
        if (
            action.tool_name == "request_return"
            and re.fullmatch(r"(?:这笔)?退货(?:退|退款)?", before)
            and not after
        ):
            return "action"
        if before in {"实付", "实付金额"} and not after and action.tool_name == "create_order":
            return "detail"
        if action.tool_name == "request_exchange":
            if before in {"补款", "待补款"} and action.amount_label == "待补款" and not after:
                return "detail"
            if (
                before in {"退差", "退款", "退款金额"}
                and action.amount_label == "退款"
                and re.fullmatch(rf"(?:{route})?", after)
            ):
                return "detail"
    return None


def whole_plan_acknowledgment(
    text: str, proposal: ActionProposal | None = None, *, step: int | None = None
) -> bool:
    """Consume complete clauses. Named actions must each identify one distinct proposed action."""
    # Parenthesized address contacts are fields; do not split currency decimals.
    ordinal = re.search(r"第\s*(\d+|[一二三])\s*步", text)
    if ordinal:
        if proposal is None or len(proposal.actions) != 1 or not step:
            return False
        number = ordinal[1] if ordinal[1].isdigit() else str("一二三".index(ordinal[1]) + 1)
        if number != str(step) or not re.search(
            r"(?:确认|执行|提交|办理)\s*$", text[: ordinal.start()]
        ):
            return False
        text = text[: ordinal.start()] + text[ordinal.end() :]
    text = re.sub(r"（(收件人[^（）]+)）", r"，\1", text)
    clauses = re.split(r"[，。；！,;!：:]|——|(?<!\d)\.|\.(?!\d)", text.strip())
    agreed = False
    named: dict[str, ProposedAction] = {}
    last_named: str | None = None
    for clause in clauses:
        clause = clause.strip()
        if not clause or clause in {"谢谢", "谢谢你", "不办理其他操作", "方案无误"}:
            continue
        if clause == "方案已确认":
            agreed = True
            continue
        if clause in {"不取消订单", "不取消其他订单"} and not (
            proposal and any(a.tool_name == "cancel_order" for a in proposal.actions)
        ):
            continue
        if match := _YES.match(clause):
            agreed = True
            clause = clause[match.end() :].strip()
            if not clause:
                continue
        if re.fullmatch(r"就这么办|就这样办|照办|(?:整份|全部|都)(?:同意|确认)", clause):
            agreed = True
            continue
        if proposal and len(proposal.actions) == 2:
            clause = re.sub(r"按(?:以上|上述|①、②)两项(?:方案)?(?:一次性)?", "按方案", clause)
            if clause in {"执行①和②", "两项都确认"}:
                agreed = True
                continue
            clause = re.sub(r"(?:这|上述|以上)?两项(?:操作|方案)?$|^两项(?=都)", "", clause)
            if not clause:
                continue
            if all(a.tool_name == "request_return" for a in proposal.actions):
                if re.fullmatch(rf"{_REQUEST}分别(?:创建|办理)两笔退货", clause):
                    continue
                if len(named) == 2:
                    if all(
                        clause == f"理由均为“{a.canonical_args['reason']}”" for a in named.values()
                    ):
                        continue
                    if clause.startswith("试算退款分别为"):
                        parts = clause.removeprefix("试算退款分别为").split("和")
                        if len(parts) != 2 or not all(
                            (m := _MONEY.fullmatch(part.strip()))
                            and _money_value(m) == a.preview_amount_cents
                            for part, a in zip(parts, named.values(), strict=True)
                        ):
                            return False
                        continue
        if re.fullmatch(rf"{_REQUEST}(?:继续)?{_EXECUTE}(?:即可|吧)?", clause):
            agreed = True
            continue
        if proposal is None:
            return False
        if (
            clause == "请执行并告知退款结果"
            and named
            and all(
                a.tool_name in {"request_return", "cancel_order", "apply_price_protection"}
                for a in proposal.actions
            )
        ):
            continue
        if re.fullmatch(rf"(?:请)?{_REFERENCE}修改", clause):
            clause = "修改地址"
        rest = re.sub(rf"^{_REQUEST}(?:同时|重试)?(?:{_EXECUTE}(?!后))?", "", clause, count=1)
        # Do not split the fixed preservation clause at its conjunction.
        rest = re.sub(r"^(?:以及|并)(?=\S)", "", rest) if named else rest
        phrases = (
            [rest] if rest.startswith("其他商品和数量") else re.split(r"并且|以及|并|和|、", rest)
        )
        for phrase in phrases:
            phrase = re.sub(r"\s+", "", phrase)
            position = re.match(r"(?:按)?方案([一二])(?:办理)?", phrase)
            if position:
                phrase = phrase[position.end() :]
            matches = [(a, _action_claim(phrase, a)) for a in proposal.actions]
            if position:
                matches = matches["一二".index(position[1]) : "一二".index(position[1]) + 1]
            candidates = [
                a
                for a, kind in matches
                if kind == "action" or (kind == "detail" and last_named in {None, a.action_id})
            ]
            if len(candidates) != 1:
                return False
            last_named = candidates[0].action_id
            named[last_named] = candidates[0]
    return (
        agreed
        and (not ordinal or bool(named))
        and (not named or (proposal is not None and len(named) == len(proposal.actions)))
    )

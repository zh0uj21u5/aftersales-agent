"""Build the business tool registry with explicit feature selection."""

from aftersales.core.config import AgentConfig
from aftersales.tools.registry import ToolRegistry


def build_registry(config: AgentConfig) -> ToolRegistry:
    from aftersales.tools import auth, escalate, query, write

    functions = [
        auth.find_user_by_phone,
        auth.find_user_by_order,
        query.get_user,
        query.list_orders,
        query.get_order,
        query.get_product,
        query.get_price_history,
        query.list_user_coupons,
        query.lookup_policy,
        query.calculate,
        write.modify_address,
        write.cancel_order,
        write.apply_price_protection,
        write.request_return,
        write.request_exchange,
        write.create_order,
        escalate.submit_approval,
        escalate.transfer_to_human,
    ]
    if config.preview_tool:
        functions.append(query.preview_refund)
    if config.todo:
        from aftersales.core.context import update_todo

        functions.append(update_todo)
    return ToolRegistry(functions)

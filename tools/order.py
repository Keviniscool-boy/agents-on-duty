from langchain_core.tools import tool

from storage.after_sales import get_order


@tool
def order_query(order_id: str, user_id: str) -> dict:
    """查询当前用户自己的演示订单。"""
    return get_order(user_id, order_id)

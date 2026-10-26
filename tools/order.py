from langchain_core.tools import tool

from tools.mock_data import ORDERS


@tool
def order_query(order_id: str) -> dict:
    """根据订单号查询订单状态和物流信息（mock 演示数据）。"""
    order = dict(ORDERS.get(order_id, {}))
    if not order:
        return {
            "order_id": order_id,
            "status": "未找到",
            "logistics": "没有找到对应订单",
            "mock": True,
        }
    return {"order_id": order_id, **order, "mock": True}

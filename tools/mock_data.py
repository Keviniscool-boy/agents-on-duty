# 全部为演示用假数据（B2）：真实订单在订单系统，不在这里。
ORDERS = {
    "1001": {
        "status": "运输中",
        "logistics": "预计明天送达",
    },
    "1002": {
        "status": "已签收",
        "logistics": "订单已于昨天签收",
    },
    # 保留历史示例单号，README 示例与 evaluation/run_mcp_eval.py 依赖它。
    "ORD-20260401-001": {
        "status": "shipped",
        "logistics": "包裹运输中",
        "amount": 299.00,
        "product": "智能理财产品A",
        "created_at": "2026-04-01 10:00:00",
    },
}

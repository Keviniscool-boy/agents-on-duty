import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

# MCP Inspector 按文件路径加载 Server 时，把项目根目录加入导入路径。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.mock_data import ORDERS

from tools.ticket_store import create_or_get_ticket

server=MCPServer(
    name="smart-cs-order-server",
    version="1.0.0",
)

@server.tool(
    name="order_query",
    description="查询订单信息（mock 演示数据，仅覆盖示例单号，非真实订单系统）",
)
async def order_query(

    order_id:str ="",
    user_id:str = "",
    request_id: str = "",
) -> dict:
    # B2：不再无视入参返回固定订单；查 mock 表并显式标注 mock，
    # 避免把演示数据当成真实业务结果输出给用户。
    key = order_id or "ORD-20260401-001"
    order = dict(ORDERS.get(key, {}))
    if not order:
        return {
            "order_id": key,
            "status": "未找到",
            "logistics": "没有找到对应订单",
            "mock": True,
        }
    order.setdefault("amount", 0.0)
    order.setdefault("product", "演示商品")
    order.setdefault("created_at", "")
    return {"order_id": key, **order, "mock": True}

#knowledge_search 
#输入查询问题，返回多个知识分段

@server.tool(
    name="knowledge_search",
    description="搜索企业知识库，返回相关文档片段（mock 模板数据，演示用）",
)
async def knowledge_search(
    query:str,
    top_k: int =3,

)->list[dict]:
    return[
        {
            "content":f"关于'{query}'的知识库文档片段",
            "source":f"FAQ.md",
            "score":0.95,
        }
    ]


#ticket_create
#输入用户问题，创建工单，返回工单ID
@server.tool(
    name="ticket_create",
    description="创建工单，返回工单ID",
)
async def ticket_create(
    title:str,
    description:str,
    priority:str="medium",
    category:str="general",
    idempotency_key: str = "",
    request_id: str = "",
)->dict:
    return create_or_get_ticket(
        title=title,
        description=description,
        priority=priority,
        category=category,
        idempotency_key=idempotency_key or request_id,
    )

#risk_check
#输入用户问题，进行风险检查，返回风险等级和建议
@server.tool(
    name="risk_check",
    description="检查交易操作的风险等级（演示实现：仅按金额阈值）",
)
async def risk_check(
    user_id:str,
    action:str,
    amount:float=0.0,
)->dict:
    risk_level="low"
    if amount>50000:
        risk_level="high"
    elif amount>10000:
        risk_level="medium"
    return {
        "user_id":user_id,
        "action":action,
        "risk_level":risk_level,
        "requires_manual_review":risk_level == "high",
    }

if __name__ == "__main__":
    import asyncio
    asyncio.run(server.run_stdio_async())

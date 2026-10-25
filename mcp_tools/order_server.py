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
    description="查询订单信息,支持按订单号或用户ID查询",
)
async def order_query(

    order_id:str ="",
    user_id:str = "",
    request_id: str = "",
) -> dict:
    return {
        "order_id":order_id or "ORD-20260401-001",
        "status":"shipped",
        "amount":299.00,
        "product":"智能理财产品A",
        "created_at":"2026-04-01 10:00:00",
    }

#knowledge_search 
#输入查询问题，返回多个知识分段

@server.tool(
    name="knowledge_search",
    description="搜索企业知识库，返回相关文档片段",
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
    description="检查交易操作的风险等级",
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

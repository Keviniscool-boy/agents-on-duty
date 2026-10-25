import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from mcp_client.client import MCPClient


SERVER_PATH = PROJECT_ROOT / "mcp_tools" / "order_server.py"
RESULTS_DIR = Path(__file__).with_name("results")


def text_content(result) -> str:
    """提取 MCP CallToolResult 中的文本内容。"""
    blocks = getattr(result, "content", [])
    return "\n".join(
        block.text
        for block in blocks
        if getattr(block, "type", "") == "text"
    )


async def main() -> None:
    client = MCPClient(str(SERVER_PATH))
    results = []

    try:
        await client.connect()
        listed = await client.list_tools()
        tool_names = [tool.name for tool in listed.tools]
        expected_tools = {
            "order_query",
            "knowledge_search",
            "ticket_create",
            "risk_check",
        }
        results.append({
            "case": "tool_list",
            "passed": expected_tools.issubset(set(tool_names)),
            "actual": tool_names,
        })

        order = await client.call_tool(
            "order_query",
            {"order_id": "ORD-20260401-001"},
        )
        order_text = text_content(order)
        results.append({
            "case": "order_query",
            "passed": "ORD-20260401-001" in order_text and "shipped" in order_text,
            "actual": order_text,
        })

        knowledge = await client.call_tool(
            "knowledge_search",
            {"query": "退款政策", "top_k": 3},
        )
        knowledge_text = text_content(knowledge)
        results.append({
            "case": "knowledge_search",
            "passed": "退款政策" in knowledge_text and "score" in knowledge_text,
            "actual": knowledge_text,
        })

        ticket = await client.call_tool(
            "ticket_create",
            {
                "title": "物流长时间未更新",
                "description": "请核查订单物流记录",
                "priority": "high",
                "category": "logistics",
            },
        )
        ticket_text = text_content(ticket)
        results.append({
            "case": "ticket_create",
            "passed": "ticket_id" in ticket_text and "created" in ticket_text,
            "actual": ticket_text,
        })

        for amount, expected_level, expected_review in (
            (0, "low", False),
            (20000, "medium", False),
            (60000, "high", True),
        ):
            risk = await client.call_tool(
                "risk_check",
                {"user_id": "eval-user", "action": "purchase", "amount": amount},
            )
            risk_text = text_content(risk)
            results.append({
                "case": f"risk_check_{amount}",
                "passed": (
                    f'"risk_level": "{expected_level}"' in risk_text
                    and f'"requires_manual_review": {str(expected_review).lower()}' in risk_text
                ),
                "actual": risk_text,
            })
    finally:
        await client.close()

    passed = sum(item["passed"] for item in results)
    summary = {
        "metric": "mcp_protocol_evaluation",
        "total": len(results),
        "passed": passed,
        "accuracy_percent": round(passed / len(results) * 100, 2),
        "results": results,
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    output_path = RESULTS_DIR / "mcp-results.json"
    output_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for item in results:
        print(f"{item['case']}: passed={item['passed']}")
    print(f"\nMCP 测试通过率: {summary['accuracy_percent']}%")
    print(f"详细结果: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())

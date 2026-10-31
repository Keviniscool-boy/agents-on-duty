import asyncio
from pathlib import Path

from mcp_client.client import MCPClient
from storage.after_sales import list_orders


PROJECT_ROOT = Path(__file__).resolve().parent
SERVER_PATH = PROJECT_ROOT / "mcp_tools" / "order_server.py"


async def main():
    client = MCPClient(str(SERVER_PATH))

    try:
        await client.connect()

        tools = await client.list_tools()

        print("可用工具：")
        for tool in tools.tools:
            print(tool.name)

        demo_user = "mcp-demo-user"
        order_id = list_orders(demo_user)[0]["order_id"]
        result = await client.call_tool(
            "order_query",
            {
                "order_id": order_id,
                "user_id": demo_user,
            },
        )

        print("工具返回：")
        print(result)

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())

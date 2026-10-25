import asyncio
from pathlib import Path

from mcp_client.client import MCPClient


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

        result = await client.call_tool(
            "order_query",
            {
                "order_id": "ORD-20260401-001",
            },
        )

        print("工具返回：")
        print(result)

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
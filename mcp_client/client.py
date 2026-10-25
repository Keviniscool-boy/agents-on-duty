import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import (
    StdioServerParameters,
    stdio_client,
)


class MCPClient:
    """连接并调用本地 MCP Server。"""

    def __init__(self, server_path: str):
        self.server_path = Path(server_path).resolve()
        self.project_root = self.server_path.parents[1]

        # 管理 stdio 连接和 ClientSession 的生命周期。
        self._exit_stack = AsyncExitStack()
        self.session: ClientSession | None = None

    async def connect(self) -> None:
        """启动 MCP Server，并完成 Client 初始化。"""
        server_params = StdioServerParameters(
            command=sys.executable,
            args=[str(self.server_path)],
            cwd=str(self.project_root),
        )

        read_stream, write_stream = (
            await self._exit_stack.enter_async_context(
                stdio_client(server_params)
            )
        )

        self.session = await self._exit_stack.enter_async_context(
            ClientSession(
                read_stream,
                write_stream,
            )
        )

        await self.session.initialize()

    async def list_tools(self):
        """获取 MCP Server 提供的工具列表。"""
        if self.session is None:
            raise RuntimeError("MCP Client has not connected")

        return await self.session.list_tools()

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ):
        """调用指定 MCP 工具。"""
        if self.session is None:
            raise RuntimeError("MCP Client has not connected")

        return await self.session.call_tool(
            name,
            arguments,
        )

    async def close(self) -> None:
        """关闭 MCP Server 进程和 Client 连接。"""
        await self._exit_stack.aclose()
        self.session = None
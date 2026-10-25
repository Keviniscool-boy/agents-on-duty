import asyncio
import hashlib
import json
import uuid

from langchain_core.messages import SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI

from mcp_client.client import MCPClient
from tools.order import order_query
from tools.ticket import ticket_create
from tracing.otel_config import trace_agent_call


class TicketHandlerAgent:
    def __init__(
        self,
        llm: ChatOpenAI,
        mcp_client: MCPClient,
    ):
        self.llm = llm
        self.mcp_client = mcp_client

    async def _call_mcp_tool(
        self,
        tool_name: str,
        arguments: dict,
    ) -> dict:
        """通过 MCP Client 调用工具，并提取工具返回的数据。"""
        result = await self.mcp_client.call_tool(
            tool_name,
            arguments,
        )

        if result.is_error:
            raise RuntimeError(f"MCP tool failed: {tool_name}")

        for item in result.content:
            if getattr(item, "type", None) == "text":
                return json.loads(item.text)

        if result.structuredContent:
            return result.structuredContent

        return {}

    @staticmethod
    def _local_tool(tool_name: str, arguments: dict) -> dict:
        """运行时 MCP 失败后的本地备用工具。"""
        if tool_name == "order_query":
            return order_query.invoke(
                {key: value for key, value in arguments.items() if key != "request_id"}
            )
        if tool_name == "ticket_create":
            # LLM 可能多传无关字段，白名单过滤防止 TypeError。
            allowed = {"title", "description", "priority", "category", "idempotency_key"}
            return ticket_create(**{key: arguments[key] for key in allowed if key in arguments})
        raise RuntimeError(f"没有本地备用工具: {tool_name}")

    async def _call_with_retry(
        self,
        tool_name: str,
        arguments: dict,
    ) -> tuple[dict, int]:
        """只对网络类异常做有限重试，业务错误不重试。"""
        retry_delays = (1, 2)
        for retry_count in range(len(retry_delays) + 1):
            try:
                return (
                    await self._call_mcp_tool(tool_name, arguments),
                    retry_count,
                )
            except (TimeoutError, ConnectionError):
                if retry_count == len(retry_delays):
                    raise
                await asyncio.sleep(retry_delays[retry_count])

    @staticmethod
    def build_idempotency_key(user_id: str, arguments: dict) -> str:
        """按业务语义（用户 + 工单内容指纹）生成幂等键。

        不使用本次 tool_call id，这样重试、降级和用户重复提交
        同一工单都命中同一条幂等记录。
        """
        fingerprint = json.dumps(
            {
                "title": arguments.get("title", ""),
                "description": arguments.get("description", ""),
                "category": arguments.get("category", "general"),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
        return f"{user_id or 'anonymous'}:{digest}"

    @trace_agent_call("ticket_handler")
    async def process(self, state: dict):
        messages = [
            SystemMessage(
                content=(
                    "你是订单客服 Agent。"
                    "用户询问订单状态或物流时，必须调用 order_query 工具。"
                    "如果用户没有提供订单号，请先询问订单号。"
                    "不要编造订单信息。"
                )
            ),
            *state["messages"],
        ]

        # 从 MCP Server 获取工具描述，并转换成 OpenAI function schema。
        mcp_tools = await self.mcp_client.list_tools()
        llm_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema,
                },
            }
            for tool in mcp_tools.tools
        ]
        llm_with_tools = self.llm.bind_tools(llm_tools)

        # 第一次调用由 LLM 决定是否需要使用 MCP 工具。
        response = await llm_with_tools.ainvoke(messages)

        if not response.tool_calls:
            return {
                "messages": [response],
                "final_response": response.content,
                "sub_results": {
                    **state.get("sub_results", {}),
                    "ticket_handler": response.content,
                },
            }

        messages.append(response)
        tool_messages = []
        tool_results = []

        for tool_call in response.tool_calls:
            print(
                f"[TicketHandlerAgent] 调用 MCP 工具: {tool_call['name']}"
            )
            print(
                f"[TicketHandlerAgent] 工具参数: {tool_call['args']}"
            )

            tool_name = tool_call["name"]
            tool_arguments = dict(tool_call["args"])
            request_id = tool_call["id"] or str(uuid.uuid4())
            tool_arguments.setdefault("request_id", request_id)
            if tool_name == "ticket_create":
                tool_arguments["idempotency_key"] = self.build_idempotency_key(
                    state.get("user_id", ""),
                    tool_arguments,
                )

            fallback_used = False
            error_type = None
            retry_count = 0
            try:
                result, retry_count = await self._call_with_retry(
                    tool_name,
                    tool_arguments,
                )
            except TimeoutError as error:
                error_type = "timeout"
                fallback_used = True
                result = self._local_tool(tool_name, tool_arguments)
            except (ConnectionError, RuntimeError) as error:
                error_type = "mcp_error"
                fallback_used = True
                result = self._local_tool(tool_name, tool_arguments)

            tool_results.append(
                {
                    "tool": tool_name,
                    "request_id": request_id,
                    "error_type": error_type,
                    "fallback_used": fallback_used,
                    "retry_count": retry_count,
                    "result": result,
                }
            )
            tool_message = ToolMessage(
                content=json.dumps(result, ensure_ascii=False),
                tool_call_id=tool_call["id"],
            )
            messages.append(tool_message)
            tool_messages.append(tool_message)

        # 第二次调用把 MCP 工具结果交给 LLM，生成面向用户的回答。
        final_response = await llm_with_tools.ainvoke(messages)

        return {
            "messages": [response, *tool_messages, final_response],
            "final_response": final_response.content,
            "sub_results": {
                **state.get("sub_results", {}),
                "ticket_handler": final_response.content,
                "ticket_handler_tool_results": tool_results,
            },
        }

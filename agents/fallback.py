from typing import Any

from tracing.otel_config import trace_agent_call


class FallbackAgent:
    """处理系统暂不支持或无法识别的用户请求。"""

    @trace_agent_call("fallback")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        response = "抱歉，我暂时无法识别或处理这个问题。"
        return {
            "final_response": response,
            "sub_results": {
                **state.get("sub_results", {}),
                "fallback": response,
            },
        }

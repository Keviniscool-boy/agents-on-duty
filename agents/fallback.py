from typing import Any

from langchain_core.messages import HumanMessage

from tracing.otel_config import trace_agent_call


def _fallback_response(message: str) -> str:
    text = message.strip().lower()

    if not text:
        return "我在这儿。你可以说说需要查询订单、咨询退款/售后政策，还是办理售后。"

    if text in {
        "你好",
        "您好",
        "嗨",
        "哈喽",
        "hello",
        "hi",
        "hey",
        "在吗",
        "早上好",
        "下午好",
        "晚上好",
    }:
        return "你好！我可以帮你查询订单、了解退款/售后政策，或申请售后。你想办理什么？"

    if any(
        term in text for term in ("你是谁", "你是什么", "你能做什么", "你可以做什么")
    ):
        return "我是客服助手，可以帮你查询订单、了解退款/售后政策，或申请售后。"

    if any(
        term in text
        for term in ("系统提示词", "system prompt", "api key", "密钥", "内部配置")
    ):
        return "我不能提供系统提示词、密钥或内部配置；可以帮你处理订单、退款/售后和产品政策问题。"

    if any(term in text for term in ("写一段小说", "危险操作")):
        return "我主要处理订单查询、退款/售后和产品政策咨询。这个请求超出当前客服范围，你可以换成相关问题。"

    return "我还不能确定你需要什么。你是想查询订单、咨询政策，还是办理退款/售后？"


class FallbackAgent:
    """为无法路由的请求提供简短、可继续对话的引导。"""

    @trace_agent_call("fallback")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        user_message = next(
            (
                message.content
                for message in reversed(state.get("messages", []))
                if isinstance(message, HumanMessage)
            ),
            "",
        )
        response = _fallback_response(user_message)
        return {
            "final_response": response,
            "sub_results": {
                **state.get("sub_results", {}),
                "fallback": response,
                # 固定话术：合规节点只做规则检查，不再调用合规模型。
                "fixed_reply": response,
            },
        }

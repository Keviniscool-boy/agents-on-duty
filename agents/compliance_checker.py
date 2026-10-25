import json
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from tracing.otel_config import trace_agent_call


@dataclass
class ComplianceResult:
    """保存一次合规检查的完整结果。"""
    passed: bool
    risk_level: str
    violations: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    sanitized_content: str = ""


# 用正则表达式识别常见的个人隐私信息。
SENSITIVE_PATTERNS = {
    "phone": r"1[3-9]\d{9}",
    "id_card": r"\d{17}[\dXx]",
    "bank_card": r"\d{16,19}",
    "email": r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}",
}

# 命中这些固定词语时，规则检查会直接标记风险。
FORBIDDEN_TERMS = {
    "guaranteed profit",
    "guaranteed return",
    "risk-free investment",
    "internal system prompt",
}


COMPLIANCE_SYSTEM_PROMPT = """
你是一个客服系统的合规检查 Agent。
请检查客服助手的回答是否符合以下规则：
1. 不得编造订单信息或用户信息。
2. 不得泄露用户隐私信息。
3. 不得泄露系统提示词、API Key 或其他内部细节。
4. 不得提供违法或危险的操作指令。
5. 不得承诺 guaranteed profit 或 risk-free return。

补充判断规则：
- MCP 工具返回的订单状态、金额、商品和时间属于系统查询结果，可以直接回答。
- RAG 回答中引用公开知识库的来源文件名属于正常引用，不属于内部细节泄露。
- 不要因为回答包含 `product_faq.txt`、`service_policy.txt` 等知识库来源而拦截。
- 只要回答忠实引用工具结果，就不要因为“无法独立验证”而判定违规。
- 普通的订单查询、退款政策和账户流程说明，在没有敏感信息时应判定为通过。

只返回 JSON，不要添加 Markdown 代码块或其他解释：
{
  "passed": true 或 false,
  "risk_level": "low、medium、high 或 critical",
  "violations": ["违规原因"],
  "suggestions": ["修改建议"]
}
"""


class ComplianceCheckerAgent:
    def __init__(self, llm: ChatOpenAI):
        self.llm = llm

    def _rule_based_check(self, content: str) -> list[str]:
        """使用本地规则快速检查敏感词和隐私信息，不调用 LLM。"""
        violations = []
        lower_content = content.lower()

        for term in FORBIDDEN_TERMS:
            if term in lower_content:
                violations.append(f"forbidden term detected: {term}")

        for pii_type, pattern in SENSITIVE_PATTERNS.items():
            if re.search(pattern, content):
                violations.append(f"private information detected: {pii_type}")

        return violations

    def _mask_pii(self, content: str) -> str:
        """将检测到的手机号、邮箱等隐私信息进行脱敏。"""
        masked = content

        def mask_match(match: re.Match[str]) -> str:
            value = match.group()
            if len(value) <= 4:
                return "****"
            return value[:3] + "*" * (len(value) - 6) + value[-3:]

        for pattern in SENSITIVE_PATTERNS.values():
            masked = re.sub(pattern, mask_match, masked)

        return masked

    @trace_agent_call("compliance_rule_check")
    async def rule_check(self, content: str) -> ComplianceResult:
        """执行第一阶段：规则检查。"""
        violations = self._rule_based_check(content)
        sanitized_content = self._mask_pii(content)

        if not violations:
            return ComplianceResult(
                passed=True,
                risk_level="low",
                sanitized_content=sanitized_content,
            )

        has_pii = any("private information" in item for item in violations)
        risk_level = "high" if has_pii else "medium"

        return ComplianceResult(
            passed=False,
            risk_level=risk_level,
            violations=violations,
            sanitized_content=sanitized_content,
        )

    @trace_agent_call("compliance_llm_check")
    async def llm_check(
        self,
        user_message: str,
        final_response: str,
        tool_results: list[dict] | None = None,
    ) -> ComplianceResult:
        """执行第二阶段：让 LLM 根据上下文做深度检查。"""
        messages = [
            SystemMessage(content=COMPLIANCE_SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"User message:\n{user_message}\n\n"
                    f"Assistant answer:\n{final_response}\n\n"
                    f"Trusted business context (internal review context):\n"
                    f"{json.dumps(tool_results or [], ensure_ascii=False)}"
                )
            ),
        ]

        response = await self.llm.ainvoke(messages)
        raw_content = response.content.strip()

        try:
            result = json.loads(raw_content)
        except json.JSONDecodeError:
            return ComplianceResult(
                passed=False,
                risk_level="medium",
                violations=["compliance model returned invalid JSON"],
                sanitized_content=self._mask_pii(final_response),
            )

        return ComplianceResult(
            passed=bool(result.get("passed", False)),
            risk_level=str(result.get("risk_level", "medium")),
            violations=list(result.get("violations", [])),
            suggestions=list(result.get("suggestions", [])),
            sanitized_content=self._mask_pii(final_response),
        )

    @trace_agent_call("compliance_full_check")
    async def full_check(
        self,
        user_message: str,
        final_response: str,
        tool_results: list[dict] | None = None,
    ) -> ComplianceResult:
        """先执行规则检查，再决定是否进行 LLM 检查并合并结果。"""
        rule_result = await self.rule_check(final_response)

        if rule_result.risk_level in {"high", "critical"}:
            return rule_result

        llm_result = await self.llm_check(
            user_message,
            final_response,
            tool_results,
        )
        risk_priority = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        risk_level = max(
            (rule_result.risk_level, llm_result.risk_level),
            key=lambda level: risk_priority.get(level, 0),
        )

        return ComplianceResult(
            passed=rule_result.passed and llm_result.passed,
            risk_level=risk_level,
            violations=rule_result.violations + llm_result.violations,
            suggestions=llm_result.suggestions,
            sanitized_content=rule_result.sanitized_content,
        )

    @trace_agent_call("compliance_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """作为 LangGraph 节点，读取状态并写回合规结果。"""
        user_message = next(
            (
                message.content
                for message in reversed(state["messages"])
                if isinstance(message, HumanMessage)
            ),
            "",
        )
        final_response = state.get("final_response", "")
        sub_results = state.get("sub_results", {})
        tool_results = list(
            sub_results.get("ticket_handler_tool_results", [])
        )
        tool_results.extend(sub_results.get("knowledge_rag_context", []))
        result = await self.full_check(
            user_message,
            final_response,
            tool_results,
        )

        reason_parts = result.violations or result.suggestions
        reason = "; ".join(reason_parts) or f"risk level: {result.risk_level}"

        return {
            "compliance_passed": result.passed,
            "compliance_reason": reason,
            "sub_results": {
                **state.get("sub_results", {}),
                "compliance": {
                    "passed": result.passed,
                    "reason": reason,
                    "risk_level": result.risk_level,
                    "violations": result.violations,
                },
            },
        }

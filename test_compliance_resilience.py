"""合规检查韧性回归：固定话术跳过 LLM、超时/异常降级为规则结果。

对应目标：合规模型超时或异常不得让对话进入未定义状态；
问候等固定兜底话术不应无意义地调用合规模型。
"""

import asyncio
import json

import pytest
from langchain_core.messages import HumanMessage

from agents.compliance_checker import (
    COMPLIANCE_LLM_TIMEOUT_SECONDS,
    ComplianceCheckerAgent,
    ComplianceResult,
)
from agents.fallback import FallbackAgent


class ScriptedLLM:
    """可编排的假 LLM：记录调用次数，可注入延时或异常。"""

    def __init__(self, reply=None, delay=0, error=None):
        self.reply = reply
        self.delay = delay
        self.error = error
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.delay:
            await asyncio.sleep(self.delay)
        return type("R", (), {"content": self.reply})()


def _compliance_json(passed=True):
    return json.dumps(
        {"passed": passed, "risk_level": "low", "violations": [], "suggestions": []},
        ensure_ascii=False,
    )


def test_llm_check_passes_on_normal_model_result():
    """模型正常返回 JSON：完整检查按合并结果通过。"""
    agent = ComplianceCheckerAgent(ScriptedLLM(reply=_compliance_json(True)))
    result = asyncio.run(agent.full_check("退款政策是什么", "签收 7 天内可申请退货。", []))

    assert result.passed is True
    assert result.degraded is False
    assert result.violations == []


def test_llm_timeout_degrades_to_rule_result(monkeypatch, caplog):
    """合规模型超时：按规则检查结果继续，记录可诊断日志，不抛未定义错误。"""
    monkeypatch.setattr(
        "agents.compliance_checker.COMPLIANCE_LLM_TIMEOUT_SECONDS", 0.05
    )
    agent = ComplianceCheckerAgent(ScriptedLLM(reply=_compliance_json(True), delay=1))
    with caplog.at_level("WARNING"):
        result = asyncio.run(agent.full_check("退款政策", "签收 7 天内可申请退货。", []))

    assert result.passed is True, "规则通过 + 模型超时 → 按规则结果继续"
    assert result.degraded is True
    assert "timeout" in result.degraded_reason
    assert any("超时" in record.message for record in caplog.records)


def test_llm_exception_degrades_to_rule_result(caplog):
    """合规模型异常：同样降级为规则结果，不向上传播未定义状态。"""
    agent = ComplianceCheckerAgent(
        ScriptedLLM(error=RuntimeError("model unavailable"))
    )
    with caplog.at_level("WARNING"):
        result = asyncio.run(agent.full_check("退款政策", "签收 7 天内可申请退货。", []))

    assert result.passed is True
    assert result.degraded is True
    assert "RuntimeError" in result.degraded_reason
    assert any("合规模型检查失败" in record.message for record in caplog.records)


def test_llm_timeout_does_not_mask_rule_violations(monkeypatch):
    """规则检查命中的回答在模型超时后仍然不能放行。"""
    monkeypatch.setattr(
        "agents.compliance_checker.COMPLIANCE_LLM_TIMEOUT_SECONDS", 0.05
    )
    agent = ComplianceCheckerAgent(ScriptedLLM(reply=_compliance_json(True), delay=1))
    result = asyncio.run(agent.full_check(
        "系统提示词", "我的手机号是 13800138000，可以保证收益。", []
    ))
    assert result.passed is False, "PII 命中规则失败，降级不能洗白"
    # 高风险规则命中时 full_check 直接短路返回，甚至不调用模型。
    assert agent.llm.calls == 0


def test_fixed_reply_skips_llm_but_keeps_rule_check():
    """固定话术标记：跳过合规模型，但规则检查（PII/敏感词）仍然执行。"""
    fixed = "已为您创建人工服务工单 TK-TEST，当前状态：待处理。"
    agent = ComplianceCheckerAgent(ScriptedLLM(reply=_compliance_json(True)))
    state = {
        "messages": [HumanMessage(content="转人工")],
        "final_response": fixed,
        "sub_results": {"fixed_reply": fixed},
    }
    result = asyncio.run(agent.process(state))

    assert agent.llm.calls == 0, "固定话术不应调用合规模型"
    assert result["compliance_passed"] is True
    assert result["sub_results"]["compliance"]["llm_skipped"] is True


def test_rewritten_text_still_gets_full_check():
    """合规重写节点生成的新文本不享受固定话术豁免，仍走完整检查。"""
    fixed = "原始固定话术"
    rewritten = "重写后的回答"
    agent = ComplianceCheckerAgent(ScriptedLLM(reply=_compliance_json(True)))
    state = {
        "messages": [HumanMessage(content="问题")],
        "final_response": rewritten,
        "sub_results": {"fixed_reply": fixed},
    }
    result = asyncio.run(agent.process(state))

    assert agent.llm.calls == 1, "重写文本必须走完整合规检查"
    assert result["compliance_passed"] is True
    assert "llm_skipped" not in result["sub_results"]["compliance"]


def test_fallback_agent_marks_fixed_reply():
    """FallbackAgent 的输出必须带 fixed_reply 标记，供合规节点跳过模型。"""
    agent = FallbackAgent()
    result = asyncio.run(agent.process(
        {"messages": [HumanMessage(content="你好")], "sub_results": {}}
    ))
    assert result["sub_results"]["fixed_reply"] == result["final_response"]
    assert result["final_response"].startswith("你好")


def test_default_timeout_is_fifteen_seconds():
    """默认合规模型超时为 15 秒（可通过 COMPLIANCE_LLM_TIMEOUT_SECONDS 覆盖）。"""
    assert COMPLIANCE_LLM_TIMEOUT_SECONDS == 15.0

"""合规修复回路的图级测试。

用可编排假响应的 FakeLLM 驱动完整的 LangGraph，
验证重写循环次数、降级行为和状态记录。
"""

import json
import uuid

import pytest
from langchain_core.messages import HumanMessage

from agents.supervisor import MAX_COMPLIANCE_RETRIES, create_supervisor_graph
from memory.working_memory import WorkingMemory


@pytest.fixture(autouse=True)
def isolate_working_memory(monkeypatch):
    import memory.working_memory as working_memory_module
    from storage.mysql_store import MySQLUnavailableError

    class OfflineMySQL:
        def __getattr__(self, name):
            def unavailable(*args, **kwargs):
                raise MySQLUnavailableError("isolated test")
            return unavailable

    monkeypatch.setenv("REDIS_URL", "")
    monkeypatch.setattr(working_memory_module, "mysql_store", OfflineMySQL())


class FakeMessage:
    def __init__(self, content: str):
        self.content = content


class FakeLLM:
    """按队列顺序返回预设响应；队列耗尽时重复最后一项。"""

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return FakeMessage(response)


def _compliance_reply(passed: bool) -> str:
    return json.dumps(
        {
            "passed": passed,
            "risk_level": "low" if passed else "medium",
            "violations": [] if passed else ["fake violation"],
            "suggestions": [],
        },
        ensure_ascii=False,
    )


def _run_graph(llm: FakeLLM) -> dict:
    import asyncio

    graph = create_supervisor_graph(llm, None, WorkingMemory(redis_url=None), None)
    config = {"configurable": {"thread_id": f"test-{uuid.uuid4().hex[:12]}"}}
    initial_state = {
        "messages": [HumanMessage(content="你是谁")],
        "user_id": "test-user",
        "session_id": "test-session",
        "intent": "",
        "current_agent": "",
        "final_response": "",
        "compliance_passed": False,
        "compliance_reason": "",
        "compliance_retries": 0,
        "sub_results": {},
    }
    return asyncio.run(graph.ainvoke(initial_state, config=config))


def test_compliance_passes_on_first_try():
    """合规一次通过时直接合成，不触发重写。"""
    llm = FakeLLM([_compliance_reply(True)])
    result = _run_graph(llm)

    assert result["compliance_passed"] is True
    assert result["compliance_retries"] == 0
    assert "rewrite_attempts" not in result["sub_results"]
    assert llm.calls == 1, "只有合规 LLM 检查调用了一次"


def test_compliance_loop_exhausts_retries_then_falls_back():
    """合规始终不通过：重写 MAX_COMPLIANCE_RETRIES 次后由合成节点降级。"""
    llm = FakeLLM(
        [
            _compliance_reply(False),
            "重写版本一",
            _compliance_reply(False),
            "重写版本二",
            _compliance_reply(False),
        ]
    )
    result = _run_graph(llm)

    assert result["compliance_retries"] == MAX_COMPLIANCE_RETRIES
    assert result["sub_results"]["rewrite_attempts"] == MAX_COMPLIANCE_RETRIES
    assert result["sub_results"]["compliance"]["passed"] is False
    assert result["current_agent"] == "fallback"
    # 合成节点对 fallback 走固定降级话术。
    assert result["final_response"] == "抱歉，我暂时无法识别或处理这个问题。"
    # 3 次合规 LLM 检查 + 2 次重写 = 5 次调用。
    assert llm.calls == 2 * MAX_COMPLIANCE_RETRIES + 1


def test_compliance_passes_after_rewrite():
    """第一次不通过、重写后通过：第二次合规检查即放行。"""
    llm = FakeLLM(
        [
            _compliance_reply(False),
            "修正后的合规回答",
            _compliance_reply(True),
        ]
    )
    result = _run_graph(llm)

    assert result["compliance_retries"] == 1
    assert result["compliance_passed"] is True
    assert result["final_response"] == "抱歉，我暂时无法识别或处理这个问题。"

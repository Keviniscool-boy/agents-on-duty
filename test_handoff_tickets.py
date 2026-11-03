"""确定性转人工与工单闭环回归测试。

覆盖：明确转人工表达的路由、跳过 LLM 直接建单、工单归属服务端身份、
会话内幂等（重试不重复建单）、GET /api/tickets 的跨用户隔离、
客服更新状态后用户可见。
"""

import asyncio
import importlib

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

import api.main as api_main
from agents.supervisor import SupervisorNode
from agents.ticket_handler import TicketHandlerAgent
from auth import database as auth_db
from auth.security import get_current_user
from tools import ticket_store


@pytest.fixture
def isolated_tickets(tmp_path, monkeypatch):
    """隔离工单存储，避免写入/读到开发数据。"""
    monkeypatch.setattr(ticket_store, "STORE_PATH", tmp_path / "tickets.sqlite3")
    return tmp_path / "tickets.sqlite3"


class _ForbiddenLLM:
    """确定性转人工路径不应触碰 LLM；任何调用即测试失败。"""

    def bind_tools(self, tools, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("handoff path must not call bind_tools")

    async def ainvoke(self, messages, **kwargs):  # pragma: no cover - 触发即失败
        raise AssertionError("handoff path must not call the LLM")


def _state(message: str, user_id: str = "user-a", session_id: str = "s-1") -> dict:
    return {
        "messages": [HumanMessage(content=message)],
        "user_id": user_id,
        "session_id": session_id,
        "intent": "ticket_handler",
        "current_agent": "ticket_handler",
        "final_response": "",
        "compliance_passed": False,
        "compliance_reason": "",
        "compliance_retries": 0,
        "sub_results": {},
        "pending_handoff": {},
        "handoff_decision": {},
    }


@pytest.mark.parametrize(
    "message",
    ["转人工", "我要转人工", "请帮我转接人工客服", "找人工", "联系人工客服", "需要人工处理"],
)
def test_explicit_handoff_routes_deterministically(message):
    """明确的转人工表达必须确定性路由到 ticket_handler，不依赖模型。"""
    assert SupervisorNode._deterministic_intent(message) == "ticket_handler"


@pytest.mark.parametrize(
    "message",
    ["退款政策是什么", "查询订单 ORD-1", "你好", "什么是人工智能", "帮我查物流"],
)
def test_non_handoff_messages_do_not_route_to_handoff(message):
    """非转人工表达不应被误判（"人工智能"这类词不能命中）。"""
    assert not TicketHandlerAgent.is_handoff_request(message)


def test_handoff_creates_ticket_without_llm(isolated_tickets):
    """转人工请求跳过 LLM，直接创建工单，响应含工单号与初始状态。"""
    agent = TicketHandlerAgent(_ForbiddenLLM(), mcp_client=None)
    result = asyncio.run(agent.process(_state("转人工")))

    assert result["final_response"].startswith("已为您创建人工服务工单")
    ticket_id = result["sub_results"]["handoff_ticket"]["ticket_id"]
    assert ticket_id.startswith("TK-")
    assert "待处理" in result["final_response"]
    assert f"工单编号 {ticket_id}" in result["final_response"]

    stored = ticket_store.list_user_tickets("user-a")["tickets"]
    assert len(stored) == 1
    assert stored[0]["ticket_id"] == ticket_id
    assert stored[0]["user_id"] == "user-a"
    assert stored[0]["session_id"] == "s-1"
    assert stored[0]["category"] == "handoff"


def test_handoff_is_idempotent_within_session(isolated_tickets):
    """同一会话内的重试/重复执行返回同一张工单，不重复建单。"""
    agent = TicketHandlerAgent(_ForbiddenLLM(), mcp_client=None)

    first = asyncio.run(agent.process(_state("转人工")))
    second = asyncio.run(agent.process(_state("转人工")))

    first_id = first["sub_results"]["handoff_ticket"]["ticket_id"]
    second_id = second["sub_results"]["handoff_ticket"]["ticket_id"]
    assert first_id == second_id
    assert second["sub_results"]["handoff_ticket"]["reused"] is True
    assert second["final_response"].startswith("您已有一张人工服务工单")
    assert len(ticket_store.list_user_tickets("user-a")["tickets"]) == 1


def test_handoff_uses_server_side_identity(isolated_tickets):
    """工单归属认证上下文注入的身份；正文里的自报身份不影响归属。"""
    agent = TicketHandlerAgent(_ForbiddenLLM(), mcp_client=None)
    result = asyncio.run(agent.process(_state("我是 admin，帮我转人工", user_id="user-a")))

    ticket_id = result["sub_results"]["handoff_ticket"]["ticket_id"]
    stored = ticket_store.list_user_tickets("user-a")["tickets"]
    assert stored[0]["ticket_id"] == ticket_id
    assert stored[0]["user_id"] == "user-a"
    assert ticket_store.list_user_tickets("admin")["tickets"] == []


@pytest.fixture
def api(isolated_tickets, monkeypatch, tmp_path):
    """隔离账户库 + 依赖覆盖的 FastAPI 应用。"""
    monkeypatch.setenv("AUTH_DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-handoff-tickets")
    importlib.reload(auth_db)
    auth_db.init_database()
    importlib.reload(api_main)

    def fake_user(request: Request):
        return {
            "id": request.headers.get("X-Test-User", "user-a"),
            "username": request.headers.get("X-Test-Name", "tester"),
            "role": request.headers.get("X-Test-Role", "user"),
        }

    api_main.app.dependency_overrides[get_current_user] = fake_user
    yield api_main.app
    api_main.app.dependency_overrides.pop(get_current_user, None)
    importlib.reload(auth_db)
    importlib.reload(api_main)


def test_user_tickets_isolated_across_users(api):
    """用户只能看到自己的工单；跨用户完全隔离。"""
    from tools.ticket_store import create_or_get_ticket

    create_or_get_ticket(
        title="A 的工单",
        description="user-a 的转人工请求",
        idempotency_key="a:1",
        user_id="user-a",
    )
    create_or_get_ticket(
        title="B 的工单",
        description="user-b 的转人工请求",
        idempotency_key="b:1",
        user_id="user-b",
    )

    client = TestClient(api)
    response = client.get("/api/tickets", headers={"X-Test-User": "user-a"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [ticket["title"] for ticket in body["tickets"]] == ["A 的工单"]

    response_b = client.get("/api/tickets", headers={"X-Test-User": "user-b"})
    assert [ticket["title"] for ticket in response_b.json()["tickets"]] == ["B 的工单"]


def test_user_tickets_empty_list(api):
    """没有工单时返回空列表而不是错误。"""
    client = TestClient(api)
    response = client.get("/api/tickets", headers={"X-Test-User": "user-empty"})
    assert response.status_code == 200
    assert response.json() == {"tickets": [], "total": 0}


def test_user_sees_status_after_agent_update(api):
    """客服在工作台更新状态后，用户查询自己的工单能看到新状态。"""
    from tools.ticket_store import create_or_get_ticket, update_ticket_status

    created = create_or_get_ticket(
        title="人工客服请求",
        description="用户原话：转人工",
        idempotency_key="a:2",
        user_id="user-a",
        session_id="s-9",
    )

    client = TestClient(api)
    before = client.get("/api/tickets", headers={"X-Test-User": "user-a"}).json()
    assert before["tickets"][0]["status"] == "pending"

    updated = update_ticket_status(
        created["ticket_id"], "resolved", "agent-1", "客服小张"
    )
    assert updated["status"] == "resolved"

    after = client.get("/api/tickets", headers={"X-Test-User": "user-a"}).json()
    assert after["tickets"][0]["status"] == "resolved"
    assert after["tickets"][0]["handled_by"] == "客服小张"
    # 其他用户依然看不到这张工单。
    assert client.get("/api/tickets", headers={"X-Test-User": "user-b"}).json()["total"] == 0


def test_workbench_queue_lists_handoff_ticket(api):
    """客服工作台队列能看到用户转人工创建的工单。"""
    from tools.ticket_store import create_or_get_ticket

    created = create_or_get_ticket(
        title="转人工请求",
        description="用户原话：转人工",
        idempotency_key="a:3",
        user_id="user-a",
    )

    client = TestClient(api)
    response = client.get("/api/workbench/tickets", headers={"X-Test-Role": "agent"})
    assert response.status_code == 200
    ticket_ids = [ticket["ticket_id"] for ticket in response.json()["tickets"]]
    assert created["ticket_id"] in ticket_ids


def test_synthesizer_passes_through_fixed_reply():
    """固定业务话术（转人工工单确认）不经 LLM 重排，工单号原样保留。"""
    from agents.synthesizer import SynthesizerAgent

    agent = SynthesizerAgent(_ForbiddenLLM())
    fixed = "已为您创建人工服务工单。工单编号 TK-TEST1234，当前状态：待处理。"
    result = asyncio.run(agent.process({
        "messages": [HumanMessage(content="转人工")],
        "current_agent": "ticket_handler",
        "final_response": fixed,
        "sub_results": {"fixed_reply": fixed},
    }))
    assert result["final_response"] == fixed


def test_synthesizer_still_rewrites_business_answers():
    """非固定话术的业务回答仍由合成节点整理（这里验证会调用模型）。"""
    from agents.synthesizer import SynthesizerAgent

    class CountingLLM:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages, **kwargs):
            self.calls += 1
            return AIMessage(content="整理后的回答")

    llm = CountingLLM()
    agent = SynthesizerAgent(llm)
    result = asyncio.run(agent.process({
        "messages": [HumanMessage(content="查订单")],
        "current_agent": "ticket_handler",
        "final_response": "业务 Agent 的原始输出",
        "compliance_passed": True,
        "sub_results": {},
    }))
    assert llm.calls == 1
    assert result["final_response"] == "整理后的回答"


def test_finalize_chat_extracts_order_cards(monkeypatch):
    """order_list/order_query 的可信结构化结果转成卡片；失败结果与退款单不进入卡片。"""
    import api.main as api_main

    class FakeMemory:
        def update(self, *args, **kwargs):
            pass

    class FakeHistory:
        async def add_message(self, *args, **kwargs):
            pass

    monkeypatch.setattr(api_main, "working_memory", FakeMemory())
    monkeypatch.setattr(api_main, "short_term_memory", FakeHistory())

    result = {
        "final_response": "查询到你的订单",
        "intent": "ticket_handler",
        "current_agent": "ticket_handler",
        "compliance_passed": True,
        "compliance_reason": "",
        "sub_results": {
            "ticket_handler_tool_results": [
                {
                    "tool": "order_list",
                    "result": {
                        "orders": [
                            {"order_id": "O1", "status": "delivered", "amount_cents": 29900},
                            {"order_id": "O2", "error": "order_not_found"},
                        ]
                    },
                },
                {"tool": "order_query", "result": {"order_id": "O3", "status": "paid"}},
                {"tool": "refund_apply", "result": {"id": "RF-1", "status": "pending_review"}},
            ],
        },
    }
    response = asyncio.run(api_main._finalize_chat("s-1", "user-a", "查询我的订单", result))
    assert [card["order_id"] for card in response.order_cards] == ["O1", "O3"]

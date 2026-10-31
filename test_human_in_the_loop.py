"""阶段二人工接管回路的图级与 API 级测试。

覆盖：interrupt 暂停、Command 恢复、重启恢复边界、
状态机防重复审批、角色门控、审计记录与幂等保护。
图级测试用可编排假响应的 FakeLLM 驱动完整 LangGraph；
API 级测试用 dependency_overrides 注入带角色的调用方。
"""

import asyncio
import json
import uuid

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

import api.main as api_main
from agents.supervisor import create_supervisor_graph
from auth.security import get_current_user
from memory.working_memory import WorkingMemory
from storage import after_sales as svc


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


class FakeLLM:
    """严格按调用顺序回放响应：工具调用 → 小结 → 合规 → 合成。

    响应必须用 AIMessage：ticket_handler 会把第一轮响应塞回
    messages 再喂给 LLM，非 BaseMessage 对象会炸序列化。
    """

    def __init__(self, responses: list[AIMessage]):
        self.responses = list(responses)
        self.calls = 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AFTER_SALES_DB_PATH", str(tmp_path / "after_sales.sqlite3"))
    return tmp_path / "after_sales.sqlite3"


def _order_for(user: str = "user-a") -> dict:
    return next(
        item for item in svc.list_orders(user) if item["status"] == "delivered"
    )


def _refund_llm(order_id: str, final_reply: str = "客服已批准您的退款申请，感谢耐心等待。") -> FakeLLM:
    return FakeLLM(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "refund_apply",
                        "args": {"order_id": order_id, "reason": "划痕"},
                        "id": "call-1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="已为您提交退款申请，等待人工审核。"),
            AIMessage(content=_compliance_reply(True)),
            AIMessage(content=final_reply),
        ]
    )


def _initial_state(session_id: str) -> dict:
    return {
        "messages": [HumanMessage(content="申请退款")],
        "user_id": "user-a",
        "session_id": session_id,
        "intent": "",
        "current_agent": "",
        "final_response": "",
        "compliance_passed": False,
        "compliance_reason": "",
        "compliance_retries": 0,
        "sub_results": {},
        "pending_handoff": {},
        "handoff_decision": {},
    }


def _pause_graph(order_id: str, session_id: str | None = None, checkpointer=None):
    """驱动图走到 human_review 并暂停，返回 (graph, config, session_id)。"""
    session_id = session_id or f"s-{uuid.uuid4().hex[:10]}"
    graph = create_supervisor_graph(
        _refund_llm(order_id), None, WorkingMemory(redis_url=None), None,
        checkpointer=checkpointer,
    )
    config = {"configurable": {"thread_id": session_id}}
    result = asyncio.run(graph.ainvoke(_initial_state(session_id), config=config))
    assert result.get("__interrupt__"), "创建待审核退款单后应暂停等待人工"
    return graph, config, session_id


def test_refund_request_pauses_graph_for_human_review(isolated_db):
    """Agent 创建待审核退款单 → 合规通过 → 图暂停在 human_review。"""
    order = _order_for()
    graph, config, session_id = _pause_graph(order["order_id"])

    state = asyncio.run(graph.aget_state(config))
    assert state.next == ("human_review",)
    assert state.values["pending_handoff"]["order_id"] == order["order_id"]
    assert state.values["pending_handoff"]["refund_id"]

    refund = svc.list_refunds("user-a")[0]
    assert refund["status"] == "pending_review"
    assert refund["session_id"] == session_id, "退款单应记录发起会话，供恢复定位线程"


def test_approve_resumes_graph_and_finalizes_state(isolated_db):
    """批准：状态机落库 → Command 恢复 → 合成答复反映决定。"""
    order = _order_for()
    graph, config, session_id = _pause_graph(order["order_id"])
    refund = svc.list_refunds("user-a")[0]

    assert svc.record_refund_decision(refund["id"], "approve", "包装完好", "ops-1")["status"] == "approved"
    result = asyncio.run(
        graph.ainvoke(
            Command(resume={"action": "approve", "note": "包装完好", "operator": "ops"}),
            config=config,
        )
    )
    assert "批准" in result["final_response"]
    assert result["handoff_decision"]["action"] == "approve"
    assert svc.list_refunds("user-a")[0]["status"] == "approved"
    actions = svc.list_refund_actions(refund["id"])
    assert len(actions) == 1 and actions[0]["action"] == "approve" and actions[0]["operator_id"] == "ops-1"


def test_reject_decision_produces_rejection(isolated_db):
    """拒绝：退款单与工单同转 rejected，恢复后答复告知拒绝。"""
    order = _order_for()
    graph = create_supervisor_graph(
        _refund_llm(order["order_id"], final_reply="很抱歉，您的退款申请未通过审核。"),
        None, WorkingMemory(redis_url=None), None,
    )
    config = {"configurable": {"thread_id": "s-reject"}}
    asyncio.run(graph.ainvoke(_initial_state("s-reject"), config=config))
    refund = svc.list_refunds("user-a")[0]

    svc.record_refund_decision(refund["id"], "reject", "超过演示规则", "ops-1")
    result = asyncio.run(
        graph.ainvoke(Command(resume={"action": "reject", "note": "超过演示规则", "operator": "ops"}), config=config)
    )
    assert "拒绝" in result["final_response"] or "未通过" in result["final_response"]
    assert svc.list_refunds("user-a")[0]["status"] == "rejected"


def test_need_info_keeps_refund_pending_and_ticket_processing(isolated_db):
    """需补充信息：申请保持 pending_review，工单转 processing。"""
    order = _order_for()
    graph = create_supervisor_graph(
        _refund_llm(order["order_id"], final_reply="请补充商品照片和包装状态。"),
        None, WorkingMemory(redis_url=None), None,
    )
    config = {"configurable": {"thread_id": "s-info"}}
    asyncio.run(graph.ainvoke(_initial_state("s-info"), config=config))
    refund = svc.list_refunds("user-a")[0]

    result = svc.record_refund_decision(refund["id"], "need_info", "请提供商品照片", "ops-1")
    assert result["status"] == "pending_review"
    resumed = asyncio.run(
        graph.ainvoke(Command(resume={"action": "need_info", "note": "请提供商品照片", "operator": "ops"}), config=config)
    )
    assert "补充" in resumed["final_response"]
    with svc._connect() as db:
        ticket_status = db.execute(
            "SELECT status FROM refund_tickets WHERE refund_id = ?", (refund["id"],)
        ).fetchone()[0]
    assert ticket_status == "processing"
    assert svc.list_refunds("user-a")[0]["status"] == "pending_review"


def test_duplicate_decision_is_rejected_by_state_machine(isolated_db):
    """防重复审批：第二次决定被状态机以 invalid_state 拒绝。"""
    order = _order_for()
    graph, config, session_id = _pause_graph(order["order_id"])
    refund = svc.list_refunds("user-a")[0]

    assert svc.record_refund_decision(refund["id"], "approve", "", "ops-1")["status"] == "approved"
    second = svc.record_refund_decision(refund["id"], "reject", "", "ops-1")
    assert second["error"] == "invalid_state"
    # 注意：图的暂停点仍在（未调用 resume）；防重复恢复由 API 层保证——
    # 第二次决定在 _resume_handoff 之前就被状态机以 409 拒绝。


def test_restart_recovery_resumes_from_persistent_checkpoint(isolated_db, tmp_path):
    """重启恢复边界：服务重启后同一 checkpoint 文件上的暂停点仍可恢复。"""
    order = _order_for()
    checkpoint = tmp_path / "checkpoints.sqlite3"

    async def scenario():
        import aiosqlite

        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        conn1 = await aiosqlite.connect(str(checkpoint))
        graph1 = create_supervisor_graph(
            _refund_llm(order["order_id"]), None, WorkingMemory(redis_url=None), None,
            checkpointer=AsyncSqliteSaver(conn1),
        )
        config = {"configurable": {"thread_id": "restart-1"}}
        result = await graph1.ainvoke(_initial_state("restart-1"), config=config)
        assert result.get("__interrupt__")
        await conn1.close()

        # 模拟重启：全新的图实例 + 同一份 checkpoint 文件。
        # 新实例的 LLM 调用序列从零开始，恢复后只剩合成一次调用，
        # 所以给它一个只含合成响应的独立队列。
        conn2 = await aiosqlite.connect(str(checkpoint))
        graph2 = create_supervisor_graph(
            FakeLLM([AIMessage(content="客服已批准您的退款申请，感谢耐心等待。")]),
            None, WorkingMemory(redis_url=None), None,
            checkpointer=AsyncSqliteSaver(conn2),
        )
        state = await graph2.aget_state(config)
        assert any(task.interrupts for task in state.tasks), "重启后暂停点应仍在检查点中"

        refund = svc.list_refunds("user-a")[0]
        svc.record_refund_decision(refund["id"], "approve", "ok", "ops-1")
        resumed = await graph2.ainvoke(
            Command(resume={"action": "approve", "note": "ok", "operator": "ops"}), config=config
        )
        assert "批准" in resumed["final_response"]
        await conn2.close()

    asyncio.run(scenario())


# ---------- API 级 ----------


@pytest.fixture
def api(isolated_db):
    from api.main import app

    from auth.database import init_database

    # 幂等：为真实账户库补 role 列与引导管理员（与 lifespan 行为一致）。
    init_database()

    def fake_user(request: Request):
        return {
            "id": request.headers.get("X-Test-User", "user-a"),
            "username": request.headers.get("X-Test-Name", "tester"),
            "role": request.headers.get("X-Test-Role", "user"),
        }

    app.dependency_overrides[get_current_user] = fake_user
    yield app
    app.dependency_overrides.pop(get_current_user, None)


def test_workbench_requires_agent_or_admin_role(api):
    """角色门控：普通用户访问工作台/授权接口一律 403。"""
    client = TestClient(api)
    assert client.get("/api/workbench/refunds", headers={"X-Test-Role": "user"}).status_code == 403
    assert client.get("/api/workbench/refunds", headers={"X-Test-Role": "agent"}).status_code == 200
    assert client.get("/api/workbench/refunds", headers={"X-Test-Role": "admin"}).status_code == 200
    assert client.post(
        "/api/admin/users/role", headers={"X-Test-Role": "agent"},
        json={"username": "nobody", "role": "admin"},
    ).status_code == 403
    assert client.post(
        "/api/admin/users/role", headers={"X-Test-Role": "admin"},
        json={"username": "no-such-user-xyz", "role": "agent"},
    ).status_code == 404
    assert client.post(
        "/api/admin/users/role", headers={"X-Test-Role": "admin"},
        json={"username": "nobody", "role": "superuser"},
    ).status_code == 400


def test_workbench_detail_exposes_handoff_payload(api):
    """详情页包含暂停线程的交接单与会话历史；未暂停时标记 thread_paused=False。"""
    order = _order_for()
    graph, config, session_id = _pause_graph(order["order_id"])
    api_main.graph = graph
    refund = svc.list_refunds("user-a")[0]
    try:
        client = TestClient(api)
        body = client.get(
            f"/api/workbench/refunds/{refund['id']}", headers={"X-Test-Role": "agent"}
        ).json()
        assert body["thread_paused"] is True
        assert body["handoff"]["type"] == "refund_review"
        assert body["handoff"]["refund_id"] == refund["id"]
        assert body["refund"]["reason"] == "划痕"
    finally:
        api_main.graph = None


def test_decision_endpoint_updates_state_and_resumes_thread(api, monkeypatch):
    """决定接口：状态机落库 → 恢复线程 → 答复写入会话历史；重复决定 409。"""
    order = _order_for()
    graph, config, session_id = _pause_graph(order["order_id"])
    monkeypatch.setattr(api_main, "graph", graph)
    refund = svc.list_refunds("user-a")[0]
    try:
        client = TestClient(api)
        headers = {"X-Test-Role": "agent", "X-Test-User": "user-a", "X-Test-Name": "ops"}
        response = client.post(
            f"/api/workbench/refunds/{refund['id']}/decision",
            headers=headers, json={"action": "approve", "note": "包装完好"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["resumed"] is True
        assert "批准" in body["agent_response"]
        assert body["refund"]["status"] == "approved"
        assert body["actions"][0]["action"] == "approve"

        # 恢复产生的最终答复进入客户会话历史。
        history = asyncio.run(api_main.short_term_memory.get_history(session_id))
        assert any("批准" in message["content"] for message in history if message["role"] == "assistant")

        # 重复决定：状态机拒绝，不再触发恢复。
        duplicate = client.post(
            f"/api/workbench/refunds/{refund['id']}/decision",
            headers=headers, json={"action": "reject", "note": ""},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["error"] == "invalid_state"
    finally:
        api_main.graph = None


def test_decision_on_rest_created_refund_updates_without_resume(api):
    """面板直建的退款单没有图线程：决定只更新状态，resumed=False。"""
    order = _order_for()
    svc.apply_refund("user-a", order["order_id"], "划痕", "rest-key")
    refund = svc.list_refunds("user-a")[0]
    client = TestClient(api)
    response = client.post(
        f"/api/workbench/refunds/{refund['id']}/decision",
        headers={"X-Test-Role": "agent"}, json={"action": "approve", "note": "ok"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["resumed"] is False
    assert body["refund"]["status"] == "approved"


def test_need_info_decision_requires_note(api):
    """要求补充信息时必须填写追问内容，否则 400。"""
    order = _order_for()
    svc.apply_refund("user-a", order["order_id"], "划痕", "info-key")
    refund = svc.list_refunds("user-a")[0]
    client = TestClient(api)
    response = client.post(
        f"/api/workbench/refunds/{refund['id']}/decision",
        headers={"X-Test-Role": "agent"}, json={"action": "need_info", "note": "  "},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "note_required"

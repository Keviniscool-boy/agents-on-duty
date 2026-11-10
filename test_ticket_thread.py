"""工单对话线程回归测试：用户与客服围绕工单异步对话。

权限矩阵：
  用户        → 读/写自己的工单对话；他人的工单 403；不存在的工单 404
  未认证      → 401
  客服/管理员 → 读/写任意工单对话
消息内容 1-2000 字校验；空工单返回空列表。
"""

import importlib

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

import api.main as api_main
from auth import database as auth_db
from auth.security import get_current_user
from tools import ticket_store


@pytest.fixture
def isolated_tickets(tmp_path, monkeypatch):
    monkeypatch.setattr(ticket_store, "STORE_PATH", tmp_path / "tickets.sqlite3")


@pytest.fixture
def api(isolated_tickets, monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-ticket-thread")
    importlib.reload(auth_db)
    auth_db.init_database()
    importlib.reload(api_main)

    def fake_user(request: Request):
        return {
            "id": request.headers.get("X-Test-User", "user-a"),
            "username": request.headers.get("X-Test-Name", "Kevin"),
            "role": request.headers.get("X-Test-Role", "user"),
        }

    api_main.app.dependency_overrides[get_current_user] = fake_user
    yield api_main.app
    api_main.app.dependency_overrides.pop(get_current_user, None)
    importlib.reload(auth_db)
    importlib.reload(api_main)


def _seed_ticket(user_id="user-a", key="a:1"):
    return ticket_store.create_or_get_ticket(
        title="转人工请求",
        description="用户原话：转人工",
        idempotency_key=key,
        user_id=user_id,
        username="Kevin",
    )


def test_user_reads_and_replies_own_ticket(api):
    """用户读取自己工单的对话并回复，消息带身份与时间。"""
    from tools.ticket_store import add_ticket_message

    created = _seed_ticket()
    add_ticket_message(created["ticket_id"], "agent", "您好，问题已收到。", "客服小张")

    client = TestClient(api)
    detail = client.get(
        f"/api/tickets/{created['ticket_id']}", headers={"X-Test-User": "user-a"}
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["ticket"]["ticket_id"] == created["ticket_id"]
    assert len(body["messages"]) == 1
    assert body["messages"][0]["sender_role"] == "agent"

    reply = client.post(
        f"/api/tickets/{created['ticket_id']}/messages",
        headers={"X-Test-User": "user-a"},
        json={"content": "请帮我看看处理进度"},
    )
    assert reply.status_code == 200
    assert reply.json()["message"]["sender_role"] == "user"
    assert reply.json()["message"]["sender_name"] == "Kevin"

    after = client.get(
        f"/api/tickets/{created['ticket_id']}", headers={"X-Test-User": "user-a"}
    ).json()
    assert [m["content"] for m in after["messages"]] == [
        "您好，问题已收到。",
        "请帮我看看处理进度",
    ]


def test_other_user_cannot_read_or_reply(api):
    """跨用户：读取 403、回复 403，且不产生消息。"""
    from tools.ticket_store import list_ticket_messages

    created = _seed_ticket()
    client = TestClient(api)
    assert (
        client.get(
            f"/api/tickets/{created['ticket_id']}", headers={"X-Test-User": "user-b"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/tickets/{created['ticket_id']}/messages",
            headers={"X-Test-User": "user-b"},
            json={"content": "偷看"},
        ).status_code
        == 403
    )
    assert list_ticket_messages(created["ticket_id"]) == []


def test_unauthenticated_rejected(isolated_tickets, monkeypatch, tmp_path):
    """未认证读写对话一律 401（真实认证链路，不走依赖覆盖）。"""
    monkeypatch.setenv("AUTH_DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-ticket-thread-401")
    importlib.reload(auth_db)
    auth_db.init_database()
    importlib.reload(api_main)

    created = _seed_ticket()
    client = TestClient(api_main.app)
    assert client.get(f"/api/tickets/{created['ticket_id']}").status_code == 401
    assert (
        client.post(
            f"/api/tickets/{created['ticket_id']}/messages", json={"content": "x"}
        ).status_code
        == 401
    )


def test_agent_reads_and_replies_any_ticket(api):
    """客服可读任意工单对话并回复（工作台职责）。"""
    from tools.ticket_store import list_ticket_messages

    created = _seed_ticket()
    client = TestClient(api)

    thread = client.get(
        f"/api/workbench/tickets/{created['ticket_id']}/messages",
        headers={"X-Test-Role": "agent"},
    )
    assert thread.status_code == 200
    assert thread.json()["messages"] == []

    reply = client.post(
        f"/api/workbench/tickets/{created['ticket_id']}/messages",
        headers={"X-Test-Role": "agent", "X-Test-Name": "admin"},
        json={"content": "已为您处理，请查收。"},
    )
    assert reply.status_code == 200
    assert reply.json()["message"]["sender_name"] == "admin"
    assert len(list_ticket_messages(created["ticket_id"])) == 1


def test_regular_user_cannot_use_workbench_thread(api):
    """普通用户不能走工作台对话端点。"""
    created = _seed_ticket()
    client = TestClient(api)
    assert (
        client.get(
            f"/api/workbench/tickets/{created['ticket_id']}/messages",
            headers={"X-Test-User": "user-a"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/workbench/tickets/{created['ticket_id']}/messages",
            headers={"X-Test-User": "user-a"},
            json={"content": "x"},
        ).status_code
        == 403
    )


def test_missing_ticket_is_404_and_content_validated(api):
    """不存在的工单 404；消息内容空/超长 400。"""
    client = TestClient(api)
    assert (
        client.get(
            "/api/tickets/TK-NOTHING", headers={"X-Test-User": "user-a"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/tickets/TK-NOTHING/messages",
            headers={"X-Test-User": "user-a"},
            json={"content": "hello"},
        ).status_code
        == 404
    )

    created = _seed_ticket()
    assert (
        client.post(
            f"/api/tickets/{created['ticket_id']}/messages",
            headers={"X-Test-User": "user-a"},
            json={"content": "   "},
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/tickets/{created['ticket_id']}/messages",
            headers={"X-Test-User": "user-a"},
            json={"content": "字" * 2001},
        ).status_code
        == 400
    )

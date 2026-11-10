"""运维接口权限矩阵回归测试。

/api/tools、/api/tools/call、/api/metrics 属于运维信息，
后端必须做角色校验（前端隐藏只是纵深防御的一层）。

权限矩阵（预期）：
  未认证       → 401
  普通用户     → 403
  客服(agent)  → 200（/api/tools/call 传未知工具名时为 400，证明已过角色门）
  管理员(admin)→ 200

登出回归：Bearer 与 Cookie 两种携带方式都吊销实际使用的令牌。
"""

import importlib

import pytest
from fastapi.testclient import TestClient

import api.main as api_main
from auth import database as auth_db


@pytest.fixture
def isolated_auth(tmp_path, monkeypatch):
    """隔离的账户库 + 固定 JWT 密钥；api.main 重新加载以拾取新数据库路径。"""
    monkeypatch.setenv("AUTH_DATABASE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-for-permission-matrix")
    importlib.reload(auth_db)
    auth_db.init_database()
    importlib.reload(api_main)
    yield api_main
    importlib.reload(auth_db)
    importlib.reload(api_main)


def _register(client, username, password="Secret#123"):
    response = client.post(
        "/api/auth/register", json={"username": username, "password": password}
    )
    assert response.status_code == 201, response.text
    return response.json()["access_token"]


def _make_user_with_role(client, username, role):
    token = _register(client, username)
    if role != "user":
        auth_db.set_user_role(username, role, actor_id="test-bootstrap")
    return token


OPS_ENDPOINTS = [
    ("GET", "/api/tools"),
    ("GET", "/api/metrics"),
]


@pytest.mark.parametrize("method,path", OPS_ENDPOINTS)
def test_unauthenticated_is_rejected(isolated_auth, method, path):
    """未认证访问运维接口必须 401，而不是泄漏数据。"""
    client = TestClient(isolated_auth.app)
    response = client.request(method, path)
    assert response.status_code == 401


@pytest.mark.parametrize("method,path", OPS_ENDPOINTS)
def test_regular_user_is_forbidden(isolated_auth, method, path):
    """普通用户访问运维接口必须 403（后端门控，不只靠前端隐藏）。"""
    client = TestClient(isolated_auth.app)
    token = _register(client, f"user_{path.strip('/').replace('/', '_')}")
    response = client.request(
        method, path, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 403


@pytest.mark.parametrize("method,path", OPS_ENDPOINTS)
def test_agent_is_allowed(isolated_auth, method, path):
    """客服可以查看工具列表与运行指标。"""
    client = TestClient(isolated_auth.app)
    token = _make_user_with_role(
        client, f"agent_{path.strip('/').replace('/', '_')}", "agent"
    )
    response = client.request(
        method, path, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200


@pytest.mark.parametrize("method,path", OPS_ENDPOINTS)
def test_admin_is_allowed(isolated_auth, method, path):
    """管理员可以查看工具列表与运行指标。"""
    client = TestClient(isolated_auth.app)
    token = _make_user_with_role(
        client, f"admin_{path.strip('/').replace('/', '_')}", "admin"
    )
    response = client.request(
        method, path, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200


def test_regular_user_cannot_call_tools(isolated_auth):
    """普通用户不能直接调用 MCP 工具（业务能力走 /api/orders 等端点）。"""
    client = TestClient(isolated_auth.app)
    token = _register(client, "user_tool_call")
    response = client.post(
        "/api/tools/call",
        json={"name": "order_query", "arguments": {"order_id": "whatever"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_agent_tool_call_reaches_validation(isolated_auth):
    """客服调用工具应通过角色门并进入工具名校验（未知工具 → 400 而非 403）。"""
    client = TestClient(isolated_auth.app)
    token = _make_user_with_role(client, "agent_tool_call", "agent")
    response = client.post(
        "/api/tools/call",
        json={"name": "no_such_tool", "arguments": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400


def test_logout_revokes_bearer_token(isolated_auth):
    """Bearer 登出：同一令牌立即失效。"""
    client = TestClient(isolated_auth.app)
    token = _register(client, "bearer_logout")

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200

    response = client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200

    reused = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert reused.status_code == 401, "Bearer 登出后旧令牌必须被拒绝"


def test_logout_with_both_cookie_and_bearer_revokes_both(isolated_auth):
    """同时携带 Bearer 与 Cookie：确定行为是两个有效令牌都吊销，不留可用残余。"""
    client = TestClient(isolated_auth.app)
    bearer = _register(client, "dual_logout")
    # 第二个账号的令牌放在 Cookie 里，验证双通道都吊销。
    cookie_token = _register(client, "dual_logout_cookie")

    client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {bearer}"},
        cookies={"agent_token": cookie_token},
    )

    assert (
        client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {bearer}"}
        ).status_code
        == 401
    ), "Bearer 令牌应被吊销"
    assert (
        client.get(
            "/api/auth/me", headers={"Authorization": f"Bearer {cookie_token}"}
        ).status_code
        == 401
    ), "Cookie 令牌也应被吊销（不留可用残余）"


def test_tickets_endpoint_requires_login_and_defaults_to_empty(isolated_auth):
    """/api/tickets 未认证 401；登录后默认返回自己的（空）工单列表。"""
    client = TestClient(isolated_auth.app)
    assert client.get("/api/tickets").status_code == 401
    token = _register(client, "tickets_user")
    response = client.get("/api/tickets", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"tickets": [], "total": 0}

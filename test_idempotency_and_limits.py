"""工单幂等、working memory 降级和登录限流的回归测试。"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.ticket_handler import TicketHandlerAgent
from memory.working_memory import WorkingMemory
from tools.ticket_store import create_or_get_ticket


def test_ticket_semantic_idempotency():
    """相同用户 + 相同工单内容算出同一幂等键，重复提交复用第一次结果。"""
    from agents.ticket_handler import TicketHandlerAgent

    key_first = TicketHandlerAgent.build_idempotency_key(
        "user-1",
        {"title": "退款申请", "description": "商品破损申请退款", "category": "refund"},
    )
    key_second = TicketHandlerAgent.build_idempotency_key(
        "user-1",
        {"title": "退款申请", "description": "商品破损申请退款", "category": "refund"},
    )
    assert key_first == key_second, "语义相同的工单应生成相同幂等键"

    first = create_or_get_ticket(
        title="退款申请",
        description="商品破损申请退款",
        category="refund",
        idempotency_key=key_first,
    )
    second = create_or_get_ticket(
        title="退款申请",
        description="商品破损申请退款",
        category="refund",
        idempotency_key=key_second,
    )
    assert second["reused"] is True
    assert second["ticket_id"] == first["ticket_id"]


def test_ticket_idempotency_key_differs_by_user_or_content():
    """不同用户或不同内容生成不同幂等键。"""
    from agents.ticket_handler import TicketHandlerAgent

    base = {"title": "退款申请", "description": "商品破损申请退款"}
    key_user1 = TicketHandlerAgent.build_idempotency_key("user-1", base)
    key_user2 = TicketHandlerAgent.build_idempotency_key("user-2", base)
    key_other = TicketHandlerAgent.build_idempotency_key(
        "user-1", {**base, "description": "商品发错货"}
    )
    assert key_user1 != key_user2
    assert key_user1 != key_other


def test_ticket_different_users_not_reused():
    """不同用户的相同内容是两张独立工单。"""
    import uuid

    suffix = uuid.uuid4().hex[:8]
    first = create_or_get_ticket(
        title="查询物流",
        description="物流一直未更新",
        idempotency_key=f"user-{suffix}-a:1",
    )
    second = create_or_get_ticket(
        title="查询物流",
        description="物流一直未更新",
        idempotency_key=f"user-{suffix}-b:1",
    )
    assert "reused" not in first
    assert "reused" not in second
    assert first["ticket_id"] != second["ticket_id"]


def test_local_tool_filters_unknown_args():
    """LLM 多传无关字段时本地备用工具不应崩溃。"""
    result = TicketHandlerAgent._local_tool(
        "ticket_create",
        {
            "title": "测试工单",
            "description": "测试描述",
            "unexpected_field": "应该被过滤",
            "another_one": 123,
        },
    )
    assert result["title"] == "测试工单"


def test_working_memory_fallback_without_redis():
    """Redis 不可用时 working memory 走进程内降级且行为一致。"""
    memory = WorkingMemory(redis_url=None)
    memory.update("sess-1", {"last_intent": "knowledge_rag"})
    memory.update("sess-1", {"current_agent": "knowledge_rag"})
    context = memory.get_context("sess-1")
    assert context["last_intent"] == "knowledge_rag"
    assert context["current_agent"] == "knowledge_rag"
    history = memory.get_history("sess-1")
    assert len(history) == 2
    assert "data" in history[0] and "timestamp" in history[0]
    memory.clear("sess-1")
    assert memory.get_context("sess-1") == {}


def test_login_rate_limit_blocks_after_threshold():
    """同一 IP + 用户名超过阈值后返回 429。"""
    from api.main import (
        LOGIN_MAX_ATTEMPTS,
        _check_login_rate_limit,
        _login_attempts,
    )

    key_user = f"127.0.0.1:ratelimit-test-{id(object())}"
    ip, username = key_user.split(":", 1)
    for _ in range(LOGIN_MAX_ATTEMPTS):
        _check_login_rate_limit(ip, username)

    from fastapi import HTTPException

    try:
        _check_login_rate_limit(ip, username)
    except HTTPException as exc:
        assert exc.status_code == 429
    else:
        raise AssertionError("超过限流阈值后应抛 429")
    finally:
        _login_attempts.clear()


def test_sqlite_checkpointer_creates_and_closes():
    """事件循环内能创建 AsyncSqliteSaver 并正常关闭。"""
    import asyncio

    from agents.supervisor import (
        CHECKPOINT_PATH,
        close_checkpoint,
        create_sqlite_checkpointer,
    )
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    async def run():
        checkpointer = await create_sqlite_checkpointer()
        try:
            assert isinstance(checkpointer, AsyncSqliteSaver)
            assert CHECKPOINT_PATH.exists()
        finally:
            await close_checkpoint()

    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if asyncio.iscoroutinefunction(fn):
                asyncio.run(fn())
            else:
                fn()
            print(f"PASS {name}")
    print("ALL TESTS PASSED")

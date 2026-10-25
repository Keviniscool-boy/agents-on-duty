"""本地业务工具，作为 MCP 运行时失败时的备用实现。"""

from __future__ import annotations

import uuid
from tools.ticket_store import create_or_get_ticket


def ticket_create(
    title: str,
    description: str,
    priority: str = "medium",
    category: str = "general",
    idempotency_key: str | None = None,
) -> dict:
    """创建工单；相同幂等键重复调用时返回第一次结果。"""
    return create_or_get_ticket(
        title=title,
        description=description,
        priority=priority,
        category=category,
        idempotency_key=idempotency_key or str(uuid.uuid4()),
    )

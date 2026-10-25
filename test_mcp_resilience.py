import asyncio

from mcp_tools.order_server import ticket_create as mcp_ticket_create
from tools.ticket import ticket_create as local_ticket_create


def test_local_ticket_create_is_idempotent():
    key = "test-local-idempotency"
    first = local_ticket_create("测试工单", "描述", idempotency_key=key)
    second = local_ticket_create("测试工单", "描述", idempotency_key=key)

    assert second["ticket_id"] == first["ticket_id"]
    assert second["reused"] is True


def test_mcp_ticket_create_is_idempotent():
    key = "test-mcp-idempotency"
    first = asyncio.run(
        mcp_ticket_create("测试工单", "描述", idempotency_key=key)
    )
    second = asyncio.run(
        mcp_ticket_create("测试工单", "描述", idempotency_key=key)
    )

    assert second["ticket_id"] == first["ticket_id"]
    assert second["reused"] is True


def test_mcp_and_local_tools_share_idempotency_store():
    key = "test-cross-process-idempotency"
    mcp_result = asyncio.run(
        mcp_ticket_create("跨进程工单", "描述", idempotency_key=key)
    )
    local_result = local_ticket_create("跨进程工单", "描述", idempotency_key=key)

    assert local_result["ticket_id"] == mcp_result["ticket_id"]
    assert local_result["reused"] is True

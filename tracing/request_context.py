"""请求级追踪号：随审计记录与响应头透传。"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

TRACE_HEADER = "x-trace-id"
_TRACE_HEADER_BYTES = TRACE_HEADER.encode()
_SAFE_TRACE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_request_id: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """返回当前请求的追踪号；不在请求上下文中时返回空串。"""
    return _request_id.get()


class TraceIdMiddleware:
    """纯网关层中间件：为每个请求分配或透传追踪号响应头。

    不走框架的高层中间件封装，是为了不干扰流式响应与后台任务——
    这对聊天的流式端点很关键。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = (headers.get(_TRACE_HEADER_BYTES) or b"").decode("ascii", "ignore")
        trace_id = incoming if _SAFE_TRACE_ID.match(incoming) else uuid.uuid4().hex
        token = _request_id.set(trace_id)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(
                    (_TRACE_HEADER_BYTES, trace_id.encode())
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            _request_id.reset(token)

"""Request-scoped trace ID, propagated to audit records and responses."""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar

TRACE_HEADER = "x-trace-id"
_TRACE_HEADER_BYTES = TRACE_HEADER.encode()
_SAFE_TRACE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_request_id: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    """Return the current HTTP request's trace ID (or "" outside a request)."""
    return _request_id.get()


class TraceIdMiddleware:
    """Pure ASGI middleware: assign/forward X-Trace-Id for every request.

    Pure ASGI (rather than BaseHTTPMiddleware) keeps streaming responses and
    background tasks untouched, which matters for the SSE chat endpoint.
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

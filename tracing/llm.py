"""Trace every chat-model call without leaking prompt/response content."""

from __future__ import annotations

import time

from tracing.admin_audit import current_trace, record_event
from tracing.otel_config import get_metrics


class TracedChatModel:
    """Thin wrapper around a LangChain chat model.

    Records call count/duration/error in AgentMetrics and, when a trace
    context is active, a content-free ``model`` event in the admin audit log.
    """

    def __init__(self, inner, model_name: str = ""):
        self._inner = inner
        self._model_name = model_name or getattr(inner, "model_name", "") or "llm"

    async def ainvoke(self, *args, **kwargs):
        context = current_trace()
        started = time.perf_counter()
        outcome = "success"
        detail = self._model_name
        try:
            return await self._inner.ainvoke(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised after recording
            outcome = "error"
            detail = f"{self._model_name};error={type(exc).__name__}"
            raise
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            get_metrics().record_model_call(
                self._model_name, duration_ms, outcome == "success"
            )
            if context:
                record_event(
                    kind="model",
                    action=self._model_name,
                    outcome=outcome,
                    duration_ms=duration_ms,
                    detail=detail,
                    **context,
                )

    def bind_tools(self, tools, **kwargs):
        return TracedChatModel(self._inner.bind_tools(tools, **kwargs), self._model_name)

    def __getattr__(self, name):
        return getattr(self._inner, name)

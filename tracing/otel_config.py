from __future__ import annotations

import functools
import threading
import time
from typing import Any, Callable

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        ConsoleSpanExporter,
    )

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False


_tracer = None


class AgentMetrics:
    """在进程内累计统计各 Agent 的调用指标与存储降级事件（B13）。"""

    def __init__(self):
        self._call_counts: dict[str, int] = {}
        self._total_duration: dict[str, float] = {}
        self._error_counts: dict[str, int] = {}
        # 存储层降级/恢复计数（A8/B13）：key 形如 "redis->mysql" / "recovered:redis"。
        self._degradation_counts: dict[str, int] = {}
        # R4：计数器加锁——单事件循环下可省，但存在线程池调用时防计数漂移。
        self._lock = threading.Lock()

    def record_call(
        self,
        agent_name: str,
        duration_ms: float,
        success: bool,
    ) -> None:
        with self._lock:
            self._call_counts[agent_name] = (
                self._call_counts.get(agent_name, 0) + 1
            )
            self._total_duration[agent_name] = (
                self._total_duration.get(agent_name, 0.0) + duration_ms
            )

            if not success:
                self._error_counts[agent_name] = (
                    self._error_counts.get(agent_name, 0) + 1
                )

    def record_degradation(self, from_tier: str, to_tier: str) -> None:
        """记录一次存储层降级事件（与 tracing 解耦，tracer 缺失也生效）。"""
        key = f"{from_tier}->{to_tier}"
        with self._lock:
            self._degradation_counts[key] = self._degradation_counts.get(key, 0) + 1

    def record_recovery(self, tier: str) -> None:
        """记录一次存储层恢复（回切成功）。"""
        key = f"recovered:{tier}"
        with self._lock:
            self._degradation_counts[key] = self._degradation_counts.get(key, 0) + 1

    def get_summary(self) -> dict[str, dict[str, float | int]]:
        summary = {}

        with self._lock:
            for agent_name, call_count in self._call_counts.items():
                total_duration = self._total_duration[agent_name]
                error_count = self._error_counts.get(agent_name, 0)

                summary[agent_name] = {
                    "total_calls": call_count,
                    "avg_duration_ms": total_duration / call_count,
                    "error_rate": error_count / call_count,
                }

            if self._degradation_counts:
                summary["storage_degradation"] = dict(self._degradation_counts)

        return summary


_metrics = AgentMetrics()


def init_tracer(
    service_name: str = "smart-cs-python-rebuild",
    otlp_endpoint: str | None = None,
) -> None:
    """Initialize OpenTelemetry and choose Jaeger or console export."""
    global _tracer

    if not _HAS_OTEL:
        return

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    # Jaeger exposes an OTLP gRPC endpoint on port 4317.
    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        except ImportError:
            exporter = ConsoleSpanExporter()
    else:
        exporter = ConsoleSpanExporter()

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)


def get_tracer():
    """Return the global tracer, or None when tracing is unavailable."""
    global _tracer

    if not _HAS_OTEL:
        return None

    if _tracer is None:
        _tracer = trace.get_tracer("smart-cs-python-rebuild")

    return _tracer


def get_metrics() -> AgentMetrics:
    """获取全局 Agent 指标统计器。"""
    return _metrics


def trace_agent_call(agent_name: str) -> Callable:
    """Create a decorator that records one Agent method call."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            # metrics 与 tracing 解耦（B13）：tracer 缺失时仍然记录调用指标，
            # 保证 /api/metrics 在未配置 OTEL 时也有数据。
            tracer = get_tracer()
            start_time = time.perf_counter()

            if tracer is None:
                try:
                    result = await func(*args, **kwargs)
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    _metrics.record_call(agent_name, duration_ms, True)
                    return result
                except Exception as error:
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    _metrics.record_call(agent_name, duration_ms, False)
                    raise

            span_name = f"agent.{agent_name}.{func.__name__}"
            with tracer.start_as_current_span(span_name) as span:
                span.set_attribute("agent.name", agent_name)
                span.set_attribute("agent.method", func.__name__)
                start_time = time.perf_counter()

                try:
                    result = await func(*args, **kwargs)
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    span.set_attribute("agent.duration_ms", duration_ms)
                    span.set_attribute("agent.success", True)
                    _metrics.record_call(agent_name, duration_ms, True)

                    if isinstance(result, dict):
                        span.set_attribute(
                            "agent.result_keys",
                            str(list(result.keys())),
                        )

                    return result
                except Exception as error:
                    duration_ms = (time.perf_counter() - start_time) * 1000
                    span.set_attribute("agent.duration_ms", duration_ms)
                    span.set_attribute("agent.success", False)
                    span.set_attribute("agent.error", str(error))
                    span.record_exception(error)
                    _metrics.record_call(agent_name, duration_ms, False)
                    raise

        return wrapper

    return decorator

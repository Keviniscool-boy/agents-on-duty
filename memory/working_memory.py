"""工作记忆：会话当前上下文 + 状态变更历史。

存储分层与失效语义（对应代码审查 A1/A3/A4/A8）：
- **写入是扇出（write-through）**：update() 同时写 Redis、MySQL 和
  进程内副本——每一层独立尽力而为，任何一层失败不影响其它层。
  这样任何一层故障切换时，其它层都有完整数据（A1）。
- **读取是回退链**：Redis → MySQL → 进程内，空结果视为 miss 继续
  向下找；后端恢复后，故障期落在进程内的数据仍可读（A3）。
- **Redis 故障带回切**：失败后按退避计划（首次立即、之后 1s/2s/4s…
  封顶 30s）重新探测，绝不永久放弃；恢复时打 INFO 日志（A4/A8）。
- 降级/回切全程打日志并上报 metrics 计数（A8/B13）。

并发约束（A6/A7）：同一会话按"单写者"设计；进程内副本是每进程的，
多 worker 部署时各副本视图可能分裂，依赖 Redis/MySQL 层保证一致性。
"""

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from threading import Lock
from typing import Any

import pymysql
from storage.mysql_store import MySQLUnavailableError, mysql_store


logger = logging.getLogger("memory.working_memory")

# Redis 重探退避（A4）：首次失败后下一次操作立即重探（保证一次瞬时
# 抖动最多丢一次机会），此后间隔指数增长，封顶 30 秒。
_REDIS_PROBE_BASE_SECONDS = 1.0
_REDIS_PROBE_MAX_SECONDS = 30.0

# 需要上报降级事件的指标来源，延迟导入避免 tracing 强耦合。
def _record_degradation(from_tier: str, to_tier: str) -> None:
    logger.warning("存储降级：%s -> %s", from_tier, to_tier)
    try:
        from tracing.otel_config import get_metrics

        get_metrics().record_degradation(from_tier, to_tier)
    except Exception:  # noqa: BLE001 — 指标失败绝不影响主流程
        pass


def _record_recovery(tier: str) -> None:
    logger.info("存储恢复：%s 重新可用", tier)
    try:
        from tracing.otel_config import get_metrics

        get_metrics().record_recovery(tier)
    except Exception:  # noqa: BLE001
        pass


class WorkingMemory:
    """保存会话当前上下文和最近的工作记录。"""

    def __init__(self, max_entries: int = 50, redis_url: str | None = None):
        self._max_entries = max_entries
        self._lock = Lock()
        self._store: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._context: dict[str, dict[str, Any]] = defaultdict(dict)
        self._redis = None
        self._redis_failures = 0
        self._redis_next_probe = 0.0
        if redis_url is None:
            redis_url = os.getenv("REDIS_URL")
        self._redis_url = redis_url

    def _get_redis(self):
        """获取 Redis 客户端；故障后按退避计划重探（A4），不永久放弃。"""
        if self._redis is not None:
            return self._redis
        if not self._redis_url:
            return None

        now = time.monotonic()
        if self._redis_failures and now < self._redis_next_probe:
            return None  # 退避窗口内不重探

        try:
            import redis

            client = redis.Redis.from_url(self._redis_url, decode_responses=True)
            client.ping()
            if self._redis_failures:
                _record_recovery("redis")
            self._redis = client
            self._redis_failures = 0
            return client
        except Exception:
            self._redis = None
            self._redis_failures += 1
            if self._redis_failures == 1:
                delay = 0.0  # 首次失败：下次操作立即重探
            else:
                delay = min(
                    _REDIS_PROBE_BASE_SECONDS * (2 ** (self._redis_failures - 2)),
                    _REDIS_PROBE_MAX_SECONDS,
                )
            self._redis_next_probe = time.monotonic() + delay
            _record_degradation("redis", "unavailable")
            return None

    def _mark_redis_down(self) -> None:
        """已建连接上的操作失败时调用：断开并计入退避（A4）。"""
        self._redis = None
        self._redis_failures += 1
        delay = (
            0.0
            if self._redis_failures == 1
            else min(
                _REDIS_PROBE_BASE_SECONDS * (2 ** (self._redis_failures - 2)),
                _REDIS_PROBE_MAX_SECONDS,
            )
        )
        self._redis_next_probe = time.monotonic() + delay

    def _redis_key(self, session_id: str, suffix: str) -> str:
        return f"smartcs:working_memory:{session_id}:{suffix}"

    def _write_redis(self, session_id: str, data: dict[str, Any], entry: dict[str, Any]) -> bool:
        """写 Redis 一层；失败返回 False 并标记故障（A1 扇出的一支）。"""
        import json

        client = self._get_redis()
        if client is None:
            return False
        try:
            context_key = self._redis_key(session_id, "context")
            history_key = self._redis_key(session_id, "history")
            pipe = client.pipeline()
            pipe.hset(context_key, mapping={k: json.dumps(v) for k, v in data.items()})
            pipe.expire(context_key, 86400)
            pipe.rpush(history_key, json.dumps(entry, ensure_ascii=False))
            pipe.ltrim(history_key, -self._max_entries, -1)
            pipe.expire(history_key, 86400)
            pipe.execute()
            return True
        except Exception:
            self._mark_redis_down()
            _record_degradation("redis", "mysql")
            return False

    def _write_mysql(self, session_id: str, user_id: str, merged: dict[str, Any], entry: dict[str, Any]) -> bool:
        """写 MySQL 一层；任何异常（含 SQL 执行错误）都算该层失败（A1/A2）。"""
        try:
            mysql_store.save_working_context(session_id, user_id, merged)
            mysql_store.append_working_history(
                session_id, user_id, entry, self._max_entries
            )
            return True
        except (MySQLUnavailableError, pymysql.MySQLError) as exc:
            _record_degradation("mysql", "in-process")
            logger.warning("MySQL 写入失败，本条仅存本地副本：%s", exc)
            return False

    def _write_local(self, session_id: str, data: dict[str, Any], entry: dict[str, Any]) -> None:
        """写进程内副本（始终执行，是读取回退链的最后一环，A3）。"""
        with self._lock:
            self._store[session_id].append(entry)
            if len(self._store[session_id]) > self._max_entries:
                self._store[session_id] = self._store[session_id][
                    -self._max_entries:
                ]
            self._context[session_id].update(data)

    def update(
        self,
        session_id: str,
        data: dict[str, Any],
        user_id: str = "",
    ) -> None:
        """扇出写入所有可达层（A1）；读取端靠回退链保证可见性。"""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": dict(data),
        }

        client = self._get_redis()
        redis_ok = False
        if client is not None:
            redis_ok = self._write_redis(session_id, data, entry)
            if not redis_ok:
                client = None

        # MySQL 是整体覆盖存储，写入前需要合并基。Redis 写成功时直接用
        # 本地视图做合并基（单写者约束下与 MySQL 等价，省一次读往返）；
        # Redis 失败时才读 MySQL 补全，防止覆盖丢失其它层已有的字段。
        if redis_ok:
            with self._lock:
                base = dict(self._context.get(session_id, {}))
            merged = {**base, **data}
            mysql_ok = self._write_mysql(session_id, user_id, merged, entry)
        else:
            try:
                base = mysql_store.load_working_context(session_id)
                merged = {**base, **data}
                mysql_ok = self._write_mysql(session_id, user_id, merged, entry)
            except (MySQLUnavailableError, pymysql.MySQLError) as exc:
                mysql_ok = False
                _record_degradation("mysql", "in-process")
                logger.warning("MySQL 读取失败，仅合并本地视图：%s", exc)

        if not redis_ok and not mysql_ok:
            _record_degradation("all", "in-process")

        self._write_local(session_id, data, entry)

    def get_context(self, session_id: str) -> dict[str, Any]:
        """回退链读取（A3）：Redis → MySQL → 进程内；空结果视为 miss。"""
        import json

        client = self._get_redis()
        if client is not None:
            try:
                raw = client.hgetall(self._redis_key(session_id, "context"))
                if raw:
                    return {k: json.loads(v) for k, v in raw.items()}
            except Exception:
                self._mark_redis_down()
                _record_degradation("redis", "mysql")

        try:
            context = mysql_store.load_working_context(session_id)
            if context:
                return context
        except (MySQLUnavailableError, pymysql.MySQLError):
            _record_degradation("mysql", "in-process")

        with self._lock:
            return dict(self._context.get(session_id, {}))

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        """回退链读取历史（A3）：Redis → MySQL → 进程内。"""
        import json

        client = self._get_redis()
        if client is not None:
            try:
                raw = client.lrange(self._redis_key(session_id, "history"), 0, -1)
                if raw:
                    return [
                        {"timestamp": item["timestamp"], "data": dict(item["data"])}
                        for item in (json.loads(item) for item in raw)
                    ]
            except Exception:
                self._mark_redis_down()
                _record_degradation("redis", "mysql")

        try:
            history = mysql_store.load_working_history(session_id)
            if history:
                return history
        except (MySQLUnavailableError, pymysql.MySQLError):
            _record_degradation("mysql", "in-process")

        with self._lock:
            return [
                {"timestamp": item["timestamp"], "data": dict(item["data"])}
                for item in self._store.get(session_id, [])
            ]

    def clear(self, session_id: str) -> None:
        """清除所有层的该会话数据。"""
        client = self._get_redis()
        if client is not None:
            try:
                client.delete(
                    self._redis_key(session_id, "context"),
                    self._redis_key(session_id, "history"),
                )
            except Exception:
                self._mark_redis_down()

        try:
            mysql_store.clear_working(session_id)
        except (MySQLUnavailableError, pymysql.MySQLError):
            _record_degradation("mysql", "in-process")

        with self._lock:
            self._store.pop(session_id, None)
            self._context.pop(session_id, None)

    def export_for_persistence(self, session_id: str) -> dict[str, Any]:
        """导出副本，供以后接入数据库或长期存储。"""
        return {
            "context": self.get_context(session_id),
            "history": self.get_history(session_id),
        }

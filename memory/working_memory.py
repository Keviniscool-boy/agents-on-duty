import os
from collections import defaultdict
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from storage.mysql_store import MySQLUnavailableError, mysql_store


class WorkingMemory:
    """保存会话当前上下文和最近的工作记录。

    _context 保存当前最新状态，_store 保存状态变化历史。
    写入优先级：Redis（多 worker 共享）→ MySQL（兜底，保存
    user_id/session_id）→ 进程内（最后手段，重启即失）。
    """

    def __init__(self, max_entries: int = 50, redis_url: str | None = None):
        self._max_entries = max_entries
        self._lock = Lock()
        self._store: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._context: dict[str, dict[str, Any]] = defaultdict(dict)
        self._redis = None
        if redis_url is None:
            redis_url = os.getenv("REDIS_URL")
        self._redis_url = redis_url

    def _get_redis(self):
        """懒连接 Redis，初次连接失败时返回 None 并走下一级降级。"""
        if self._redis is None and self._redis_url:
            try:
                import redis

                client = redis.Redis.from_url(self._redis_url, decode_responses=True)
                client.ping()
                self._redis = client
            except Exception:
                self._redis = None
                self._redis_url = None
        return self._redis

    def _redis_key(self, session_id: str, suffix: str) -> str:
        return f"smartcs:working_memory:{session_id}:{suffix}"

    def update(
        self,
        session_id: str,
        data: dict[str, Any],
        user_id: str = "",
    ) -> None:
        """更新会话当前上下文，并记录本次变化。"""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": dict(data),
        }

        client = self._get_redis()
        if client is not None:
            import json

            context_key = self._redis_key(session_id, "context")
            history_key = self._redis_key(session_id, "history")
            try:
                pipe = client.pipeline()
                pipe.hset(context_key, mapping={k: json.dumps(v) for k, v in data.items()})
                pipe.expire(context_key, 86400)
                pipe.rpush(history_key, json.dumps(entry, ensure_ascii=False))
                pipe.ltrim(history_key, -self._max_entries, -1)
                pipe.expire(history_key, 86400)
                pipe.execute()
                return
            except Exception:
                self._redis = None

        try:
            # MySQL 兜底：上下文合并写，历史追加并截断。
            merged = mysql_store.load_working_context(session_id)
            merged.update(data)
            mysql_store.save_working_context(session_id, user_id, merged)
            mysql_store.append_working_history(
                session_id, user_id, entry, self._max_entries
            )
            return
        except MySQLUnavailableError:
            pass

        with self._lock:
            self._store[session_id].append(entry)
            if len(self._store[session_id]) > self._max_entries:
                self._store[session_id] = self._store[session_id][
                    -self._max_entries:
                ]
            self._context[session_id].update(data)

    def get_context(self, session_id: str) -> dict[str, Any]:
        """返回当前上下文的副本，避免调用方直接修改内部数据。"""
        client = self._get_redis()
        if client is not None:
            import json

            try:
                raw = client.hgetall(self._redis_key(session_id, "context"))
                if raw:
                    return {k: json.loads(v) for k, v in raw.items()}
            except Exception:
                self._redis = None

        try:
            return mysql_store.load_working_context(session_id)
        except MySQLUnavailableError:
            pass

        with self._lock:
            return dict(self._context.get(session_id, {}))

    def get_history(self, session_id: str) -> list[dict[str, Any]]:
        """返回工作状态变化历史的副本。"""
        client = self._get_redis()
        if client is not None:
            import json

            try:
                raw = client.lrange(self._redis_key(session_id, "history"), 0, -1)
                if raw:
                    return [
                        {"timestamp": item["timestamp"], "data": dict(item["data"])}
                        for item in (json.loads(item) for item in raw)
                    ]
            except Exception:
                self._redis = None

        try:
            return mysql_store.load_working_history(session_id)
        except MySQLUnavailableError:
            pass

        with self._lock:
            return [
                {"timestamp": item["timestamp"], "data": dict(item["data"])}
                for item in self._store.get(session_id, [])
            ]

    def clear(self, session_id: str) -> None:
        """清除指定会话的工作记忆。"""
        client = self._get_redis()
        if client is not None:
            try:
                client.delete(
                    self._redis_key(session_id, "context"),
                    self._redis_key(session_id, "history"),
                )
            except Exception:
                self._redis = None

        try:
            mysql_store.clear_working(session_id)
        except MySQLUnavailableError:
            pass

        with self._lock:
            self._store.pop(session_id, None)
            self._context.pop(session_id, None)

    def export_for_persistence(self, session_id: str) -> dict[str, Any]:
        """导出副本，供以后接入数据库或长期存储。"""
        return {
            "context": self.get_context(session_id),
            "history": self.get_history(session_id),
        }

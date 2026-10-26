"""短期记忆：每个 session 最近的聊天消息。

存储分层与失效语义（对应代码审查 A1/A3/A4/A8）：
- **写入是扇出（write-through）**：add_message() 同时写 Redis、MySQL
  和进程内副本，任何一层失败不影响其它层——层间切换不丢消息（A1）。
- **读取是回退链**：Redis → MySQL → 进程内，空结果视为 miss 继续
  向下找；后端恢复后故障期消息仍可读（A3）。
- **Redis 故障带回切**：失败按退避计划重探（首次立即、之后 1s/2s/4s…
  封顶 30s），不永久放弃（A4）；降级/回切打日志并上报 metrics（A8）。

并发约束（A7）：进程内副本按进程隔离，多 worker 依赖 Redis/MySQL 层。
"""

import json
import logging
import time
from datetime import datetime

import pymysql
import redis.asyncio as aioredis

from storage.mysql_store import MySQLUnavailableError, mysql_store


logger = logging.getLogger("memory.short_term")

# Redis 重探退避（A4）：首次失败立即重探，之后指数增长封顶 30 秒。
_REDIS_PROBE_BASE_SECONDS = 1.0
_REDIS_PROBE_MAX_SECONDS = 30.0


def _record_degradation(from_tier: str, to_tier: str) -> None:
    logger.warning("存储降级：%s -> %s", from_tier, to_tier)
    try:
        from tracing.otel_config import get_metrics

        get_metrics().record_degradation(from_tier, to_tier)
    except Exception:  # noqa: BLE001
        pass


def _record_recovery(tier: str) -> None:
    logger.info("存储恢复：%s 重新可用", tier)
    try:
        from tracing.otel_config import get_metrics

        get_metrics().record_recovery(tier)
    except Exception:  # noqa: BLE001
        pass


class ShortTermMemory:
    """保存每个 session 最近的聊天消息（写扇出、读回退）。"""

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        max_turns: int = 20,
        ttl_seconds: int = 1800,
    ):
        self.redis_url = redis_url
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self.redis = None
        self._redis_failures = 0
        self._redis_next_probe = 0.0
        self._fallback_store: dict[str, list[dict]] = {}

    def _mark_redis_down(self) -> None:
        """连接或操作失败后调用：断开并进入退避（首次失败下次立即重探）。"""
        self.redis = None
        self._redis_failures += 1
        if self._redis_failures == 1:
            delay = 0.0
        else:
            delay = min(
                _REDIS_PROBE_BASE_SECONDS * (2 ** (self._redis_failures - 2)),
                _REDIS_PROBE_MAX_SECONDS,
            )
        self._redis_next_probe = time.monotonic() + delay

    async def _get_redis(self):
        """懒连接 Redis；故障后按退避计划重探，不永久放弃（A4）。"""
        if self.redis is not None:
            return self.redis

        now = time.monotonic()
        if self._redis_failures and now < self._redis_next_probe:
            return None

        try:
            client = aioredis.from_url(
                self.redis_url,
                decode_responses=True,
            )
            await client.ping()
            if self._redis_failures:
                _record_recovery("redis")
            self.redis = client
            self._redis_failures = 0
            return client
        except Exception:
            self._mark_redis_down()
            _record_degradation("redis", "unavailable")
            return None

    def _session_key(self, session_id: str) -> str:
        return f"smartcs:short_term:{session_id}"

    def _add_to_fallback(self, session_id: str, message: dict) -> None:
        """写进程内副本（读取回退链的最后一环，A3）。"""
        self._fallback_store.setdefault(session_id, []).append(message)
        self._fallback_store[session_id] = (
            self._fallback_store[session_id][-self.max_turns:]
        )

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        user_id: str = "",
    ) -> None:
        """扇出写入所有可达层（A1）：Redis、MySQL、进程内副本。"""
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }

        redis_client = await self._get_redis()
        if redis_client is not None:
            try:
                key = self._session_key(session_id)
                await redis_client.rpush(
                    key,
                    json.dumps(message, ensure_ascii=False),
                )
                await redis_client.ltrim(key, -self.max_turns, -1)
                await redis_client.expire(key, self.ttl_seconds)
            except Exception:
                self._mark_redis_down()
                _record_degradation("redis", "mysql")

        try:
            mysql_store.add_message(session_id, user_id, message)
        except (MySQLUnavailableError, pymysql.MySQLError) as exc:
            _record_degradation("mysql", "in-process")
            logger.warning("MySQL 写入失败，本条仅存本地副本：%s", exc)

        self._add_to_fallback(session_id, message)

    async def get_history(
        self,
        session_id: str,
        last_n: int | None = None,
    ) -> list[dict]:
        """回退链读取（A3）：Redis → MySQL → 进程内；空结果视为 miss。"""
        count = last_n or self.max_turns
        redis_client = await self._get_redis()

        if redis_client is not None:
            try:
                raw_messages = await redis_client.lrange(
                    self._session_key(session_id),
                    -count,
                    -1,
                )
                if raw_messages:
                    return [json.loads(message) for message in raw_messages]
            except Exception:
                self._mark_redis_down()
                _record_degradation("redis", "mysql")

        try:
            messages = mysql_store.get_messages(session_id, count)
            if messages:
                return messages
        except (MySQLUnavailableError, pymysql.MySQLError):
            _record_degradation("mysql", "in-process")

        history = self._fallback_store.get(session_id, [])
        return history[-count:]

    async def clear_history(self, session_id: str) -> None:
        redis_client = await self._get_redis()

        if redis_client is not None:
            try:
                await redis_client.delete(self._session_key(session_id))
            except Exception:
                self._mark_redis_down()

        try:
            mysql_store.clear_messages(session_id)
        except (MySQLUnavailableError, pymysql.MySQLError):
            _record_degradation("mysql", "in-process")

        self._fallback_store.pop(session_id, None)

import json
from datetime import datetime

import redis.asyncio as aioredis

from storage.mysql_store import MySQLUnavailableError, mysql_store


class ShortTermMemory:
    """保存每个 session 最近的聊天消息。

    写入优先级：Redis → MySQL 兜底（保存 user_id/session_id）→
    进程内备用存储。
    """

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
        self._fallback_store: dict[str, list[dict]] = {}

    async def _get_redis(self):
        """懒连接 Redis，初次连接失败时返回 None。"""
        if self.redis is None:
            try:
                self.redis = aioredis.from_url(
                    self.redis_url,
                    decode_responses=True,
                )
                await self.redis.ping()
            except Exception:
                self.redis = None
        return self.redis

    def _session_key(self, session_id: str) -> str:
        return f"smartcs:short_term:{session_id}"

    def _add_to_fallback(self, session_id: str, message: dict) -> None:
        """Redis 不可用时，把消息保存到进程内备用存储。"""
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
                return
            except Exception:
                self.redis = None

        try:
            mysql_store.add_message(session_id, user_id, message)
            return
        except MySQLUnavailableError:
            pass

        self._add_to_fallback(session_id, message)

    async def get_history(
        self,
        session_id: str,
        last_n: int | None = None,
    ) -> list[dict]:
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
                self.redis = None

        try:
            return mysql_store.get_messages(session_id, count)
        except MySQLUnavailableError:
            pass

        history = self._fallback_store.get(session_id, [])
        return history[-count:]

    async def clear_history(self, session_id: str) -> None:
        redis_client = await self._get_redis()

        if redis_client is not None:
            try:
                await redis_client.delete(self._session_key(session_id))
            except Exception:
                self.redis = None

        try:
            mysql_store.clear_messages(session_id)
        except MySQLUnavailableError:
            pass

        self._fallback_store.pop(session_id, None)

"""MySQL 兜底存储层。

保存所有会话上下文（含 user_id、session_id），是 Redis 与进程内
存储全部不可用时的最终保底。应用启动时必须通过 self_check()，
MySQL 连不上则拒绝启动。

设计约定（对应代码审查 A2/A5/A6/A7/A9/A10）：
- 本层所有公开方法的**任何**异常（连接失败、ping 失败、SQL 执行错误）
  都统一转换为 MySQLUnavailableError，调用方据此进入下一级兜底，
  运行时 SQL 错误不会穿透成 500（A2）。
- 连接建立、重试、置空都在 RLock 内完成（A9）；连接失败做有限
  指数退避重试（A10）。
- save_working_context 为**整体覆盖**语义，调用方负责先读后合并；
  不用 JSON_MERGE_PATCH，避免 null 值静默删键（A5）。
- 跨进程的读-改-写不原子（A6）：本层按"同一会话单写者"约束设计，
  多 worker 并发更新同一会话可能丢更新，见 README 已知限制（A7）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import pymysql


class MySQLUnavailableError(RuntimeError):
    """MySQL 自检或访问失败时抛出。调用方捕获后进入下一级兜底。"""


# 连接重试（A10）：瞬时故障退避后重试，减少请求级 MySQL 副本丢失。
_CONNECT_ATTEMPTS = 2
_CONNECT_BACKOFF_SECONDS = (0.1,)
# 熔断冷却（A10 的代价控制）：连接失败后 2 秒内的调用快速失败，
# 避免持续宕机时每个操作都重复支付重试成本。
_CONNECT_COOLDOWN_SECONDS = 2.0


class MySQLStore:
    def __init__(self):
        self._conn: pymysql.Connection | None = None
        # RLock：_get_conn 在方法已持有 _lock 时再次加锁（A9）。
        self._lock = threading.RLock()
        self._connect_cooldown_until = 0.0

    @staticmethod
    def _config() -> dict[str, Any]:
        return {
            "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
            "port": int(os.getenv("MYSQL_PORT", "3306")),
            "user": os.getenv("MYSQL_USER", "root"),
            "password": os.getenv("MYSQL_PASSWORD", ""),
            "database": os.getenv("MYSQL_DATABASE", "smart_cs"),
            "charset": "utf8mb4",
            "autocommit": True,
            "connect_timeout": 5,
        }

    def _get_conn(self) -> pymysql.Connection:
        """懒连接；失效自动重连；连接失败有限退避重试（A9/A10）。

        必须在持有 self._lock 的前提下调用（公开方法统一经 _cursor）。
        最近一次连接失败仍在冷却窗口内时快速失败，不做重试。
        """
        if self._conn is not None:
            try:
                self._conn.ping(reconnect=True)
                return self._conn
            except Exception:
                self._conn = None  # 连接已坏，走下方重建（含重试）

        now = time.monotonic()
        if now < self._connect_cooldown_until:
            raise MySQLUnavailableError("MySQL connect cooldown (recent failure)")

        last_exc: Exception | None = None
        for attempt in range(_CONNECT_ATTEMPTS):
            try:
                self._conn = pymysql.connect(**self._config())
                self._connect_cooldown_until = 0.0
                return self._conn
            except Exception as exc:
                last_exc = exc
                if attempt < _CONNECT_ATTEMPTS - 1:
                    time.sleep(_CONNECT_BACKOFF_SECONDS[min(attempt, len(_CONNECT_BACKOFF_SECONDS) - 1)])
        self._connect_cooldown_until = time.monotonic() + _CONNECT_COOLDOWN_SECONDS
        raise MySQLUnavailableError(
            f"Cannot connect to MySQL after {_CONNECT_ATTEMPTS} attempts: {last_exc}"
        ) from last_exc

    @contextmanager
    def _cursor(self) -> Iterator[pymysql.cursors.Cursor]:
        """统一的游标生命周期 + 异常转换（A2 的核心）。

        连接阶段与语句执行阶段的任何异常都转为 MySQLUnavailableError，
        执行期异常同时置空连接，下次调用重建。
        """
        with self._lock:
            try:
                conn = self._get_conn()
                cursor = conn.cursor()
            except MySQLUnavailableError:
                raise
            except Exception as exc:
                self._conn = None
                raise MySQLUnavailableError(f"MySQL access failed: {exc}") from exc
            try:
                yield cursor
            except MySQLUnavailableError:
                raise
            except Exception as exc:
                self._conn = None
                raise MySQLUnavailableError(f"MySQL operation failed: {exc}") from exc
            finally:
                cursor.close()

    def self_check(self) -> None:
        """启动自检：连不上 MySQL 直接抛错，由调用方终止启动。

        显式健康探测不适用普通路径的两条限制：
        - 不受连接冷却（熔断）约束——否则先执行的降级用例/操作触发
          冷却后，紧跟的自检会被快速失败误判为不可用（CI 上
          roundtrip 曾因此被误 skip）；
        - 自带 3 次探测重试（0.5s 间隔）——端口转发/服务就绪的瞬时
          抖动不应让门禁误报。
        """
        last_exc: Exception | None = None
        for attempt in range(3):
            self._connect_cooldown_until = 0.0
            try:
                with self._cursor() as cursor:
                    cursor.execute("SELECT 1")
                self._init_schema()
                return
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.5)
        raise MySQLUnavailableError(
            "MySQL is required but unreachable: "
            f"{self._config()['host']}:{self._config()['port']} - {last_exc}"
        ) from last_exc

    def _init_schema(self) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS working_memory_context (
                    session_id VARCHAR(64) PRIMARY KEY,
                    user_id VARCHAR(64) NOT NULL DEFAULT '',
                    context_json JSON NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS working_memory_history (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    session_id VARCHAR(64) NOT NULL,
                    user_id VARCHAR(64) NOT NULL DEFAULT '',
                    entry_json JSON NOT NULL,
                    created_at DATETIME NOT NULL,
                    INDEX idx_wm_session (session_id, id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS short_term_messages (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    session_id VARCHAR(64) NOT NULL,
                    user_id VARCHAR(64) NOT NULL DEFAULT '',
                    role VARCHAR(16) NOT NULL,
                    content MEDIUMTEXT NOT NULL,
                    created_at DATETIME NOT NULL,
                    INDEX idx_stm_session (session_id, id)
                )
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # ---------- working memory ----------

    def save_working_context(
        self,
        session_id: str,
        user_id: str,
        context: dict[str, Any],
    ) -> None:
        """整体覆盖写入（A5）：调用方先 load 合并后再调本方法。

        不使用 JSON_MERGE_PATCH——null 值会被静默删键；覆盖语义下
        null 作为普通值存储，空值语义由调用方决定。
        并发约束：同一会话单写者（A6/A7，见模块 docstring）。
        """
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO working_memory_context
                    (session_id, user_id, context_json, updated_at)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    user_id = VALUES(user_id),
                    context_json = VALUES(context_json),
                    updated_at = VALUES(updated_at)
                """,
                (
                    session_id,
                    user_id,
                    json.dumps(context, ensure_ascii=False),
                    self._now(),
                ),
            )

    def load_working_context(self, session_id: str) -> dict[str, Any]:
        with self._cursor() as cursor:
            cursor.execute(
                "SELECT context_json FROM working_memory_context WHERE session_id = %s",
                (session_id,),
            )
            row = cursor.fetchone()
        return json.loads(row[0]) if row else {}

    def append_working_history(
        self,
        session_id: str,
        user_id: str,
        entry: dict[str, Any],
        max_entries: int,
    ) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO working_memory_history
                    (session_id, user_id, entry_json, created_at)
                VALUES (%s, %s, %s, %s)
                """,
                (
                    session_id,
                    user_id,
                    json.dumps(entry, ensure_ascii=False),
                    self._now(),
                ),
            )
            # 只保留最近 max_entries 条。
            cursor.execute(
                """
                DELETE FROM working_memory_history
                WHERE session_id = %s
                  AND id NOT IN (
                    SELECT id FROM (
                        SELECT id FROM working_memory_history
                        WHERE session_id = %s
                        ORDER BY id DESC LIMIT %s
                    ) AS keep
                  )
                """,
                (session_id, session_id, max_entries),
            )

    def load_working_history(self, session_id: str) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT entry_json FROM working_memory_history
                WHERE session_id = %s ORDER BY id ASC
                """,
                (session_id,),
            )
            rows = cursor.fetchall()
        return [json.loads(row[0]) for row in rows]

    def clear_working(self, session_id: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "DELETE FROM working_memory_context WHERE session_id = %s",
                (session_id,),
            )
            cursor.execute(
                "DELETE FROM working_memory_history WHERE session_id = %s",
                (session_id,),
            )

    # ---------- short term memory ----------

    def add_message(
        self,
        session_id: str,
        user_id: str,
        message: dict[str, Any],
    ) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO short_term_messages
                    (session_id, user_id, role, content, created_at)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    session_id,
                    user_id,
                    message.get("role", ""),
                    message.get("content", ""),
                    self._now(),
                ),
            )

    def get_messages(self, session_id: str, last_n: int) -> list[dict[str, Any]]:
        with self._cursor() as cursor:
            cursor.execute(
                """
                SELECT role, content, created_at FROM short_term_messages
                WHERE session_id = %s ORDER BY id DESC LIMIT %s
                """,
                (session_id, last_n),
            )
            rows = cursor.fetchall()
        return [
            {"role": role, "content": content, "timestamp": str(created_at)}
            for role, content, created_at in reversed(rows)
        ]

    def clear_messages(self, session_id: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                "DELETE FROM short_term_messages WHERE session_id = %s",
                (session_id,),
            )


mysql_store = MySQLStore()

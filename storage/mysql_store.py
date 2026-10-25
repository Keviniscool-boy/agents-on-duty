"""MySQL 兜底存储层。

保存所有会话上下文（含 user_id、session_id），是 Redis 与进程内
存储全部不可用时的最终保底。应用启动时必须通过 self_check()，
MySQL 连不上则拒绝启动。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any

import pymysql


class MySQLUnavailableError(RuntimeError):
    """MySQL 自检或访问失败时抛出。"""


class MySQLStore:
    def __init__(self):
        self._conn: pymysql.Connection | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _config() -> dict[str, Any]:
        return {
            "host": os.getenv("MYSQL_HOST", "localhost"),
            "port": int(os.getenv("MYSQL_PORT", "3306")),
            "user": os.getenv("MYSQL_USER", "root"),
            "password": os.getenv("MYSQL_PASSWORD", ""),
            "database": os.getenv("MYSQL_DATABASE", "smart_cs"),
            "charset": "utf8mb4",
            "autocommit": True,
            "connect_timeout": 5,
        }

    def _get_conn(self) -> pymysql.Connection:
        """懒连接；连接失效时自动重连一次。"""
        if self._conn is None:
            try:
                self._conn = pymysql.connect(**self._config())
            except Exception as exc:
                raise MySQLUnavailableError(
                    f"Cannot connect to MySQL: {exc}"
                ) from exc
        try:
            self._conn.ping(reconnect=True)
        except Exception as exc:
            self._conn = None
            raise MySQLUnavailableError(f"MySQL connection lost: {exc}") from exc
        return self._conn

    def self_check(self) -> None:
        """启动自检：连不上 MySQL 直接抛错，由调用方终止启动。"""
        try:
            conn = self._get_conn()
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
            self._init_schema()
        except Exception as exc:
            raise MySQLUnavailableError(
                "MySQL is required but unreachable: "
                f"{self._config()['host']}:{self._config()['port']} - {exc}"
            ) from exc

    def _init_schema(self) -> None:
        conn = self._get_conn()
        with conn.cursor() as cursor:
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
        conn = self._get_conn()
        with self._lock, conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO working_memory_context
                    (session_id, user_id, context_json, updated_at)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    user_id = VALUES(user_id),
                    context_json = JSON_MERGE_PATCH(context_json, VALUES(context_json)),
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
        conn = self._get_conn()
        with self._lock, conn.cursor() as cursor:
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
        conn = self._get_conn()
        with self._lock, conn.cursor() as cursor:
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
        conn = self._get_conn()
        with self._lock, conn.cursor() as cursor:
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
        conn = self._get_conn()
        with self._lock, conn.cursor() as cursor:
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
        conn = self._get_conn()
        with self._lock, conn.cursor() as cursor:
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
        conn = self._get_conn()
        with self._lock, conn.cursor() as cursor:
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
        conn = self._get_conn()
        with self._lock, conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM short_term_messages WHERE session_id = %s",
                (session_id,),
            )


mysql_store = MySQLStore()

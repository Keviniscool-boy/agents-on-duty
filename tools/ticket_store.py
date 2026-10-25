"""MCP Server 与本地备用工具共享的工单幂等存储。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path


STORE_PATH = Path(__file__).resolve().parents[1] / "data" / "ticket_idempotency.sqlite3"


def _connect() -> sqlite3.Connection:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(STORE_PATH)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ticket_idempotency (
            idempotency_key TEXT PRIMARY KEY,
            result_json TEXT NOT NULL
        )
        """
    )
    return connection


def create_or_get_ticket(
    *,
    title: str,
    description: str,
    priority: str = "medium",
    category: str = "general",
    idempotency_key: str = "",
) -> dict:
    """使用唯一幂等键创建工单，重复请求返回第一次结果。"""
    key = idempotency_key or str(uuid.uuid4())
    result = {
        "ticket_id": f"TK-{uuid.uuid4().hex[:8].upper()}",
        "title": title,
        "description": description,
        "status": "created",
        "priority": priority,
        "category": category,
        "idempotency_key": key,
    }

    connection = _connect()
    try:
        row = connection.execute(
            "SELECT result_json FROM ticket_idempotency WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if row:
            return {**json.loads(row[0]), "reused": True}

        connection.execute(
            "INSERT INTO ticket_idempotency (idempotency_key, result_json) VALUES (?, ?)",
            (key, json.dumps(result, ensure_ascii=False)),
        )
        connection.commit()
        return result
    except sqlite3.IntegrityError:
        connection.rollback()
        row = connection.execute(
            "SELECT result_json FROM ticket_idempotency WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if not row:
            raise
        return {**json.loads(row[0]), "reused": True}
    finally:
        connection.close()

"""MCP Server 与本地备用工具共享的工单幂等存储。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
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
    user_id: str = "",
    session_id: str = "",
) -> dict:
    """使用唯一幂等键创建工单，重复请求返回第一次结果。"""
    key = idempotency_key or str(uuid.uuid4())
    result = {
        "ticket_id": f"TK-{uuid.uuid4().hex[:8].upper()}",
        "title": title,
        "description": description,
        "status": "pending",
        "priority": priority,
        "category": category,
        "idempotency_key": key,
        "user_id": user_id,
        "session_id": session_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
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


def list_tickets(limit: int = 100, offset: int = 0, status: str = "") -> list[dict]:
    """Return stored support tickets, including older records without metadata."""
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT result_json FROM ticket_idempotency"
        ).fetchall()
        tickets = [json.loads(row[0]) for row in rows]
        if status:
            tickets = [ticket for ticket in tickets if ticket.get("status", "created") == status]
        tickets.sort(key=lambda ticket: ticket.get("created_at", ""), reverse=True)
        return tickets[offset : offset + limit]
    finally:
        connection.close()


def update_ticket_status(
    ticket_id: str,
    status: str,
    operator_id: str,
    operator: str,
) -> dict | None:
    """Update a ticket's workbench state using a SQLite write transaction."""
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT idempotency_key, result_json FROM ticket_idempotency"
        ).fetchall()
        for row in rows:
            ticket = json.loads(row[1])
            if ticket.get("ticket_id") != ticket_id:
                continue
            ticket["status"] = status
            ticket["updated_at"] = datetime.now(timezone.utc).isoformat()
            ticket["handled_by_id"] = operator_id
            ticket["handled_by"] = operator
            connection.execute(
                "UPDATE ticket_idempotency SET result_json = ? WHERE idempotency_key = ?",
                (json.dumps(ticket, ensure_ascii=False), row[0]),
            )
            connection.commit()
            return ticket
        connection.rollback()
        return None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

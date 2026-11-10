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
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ticket_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT NOT NULL,
            sender_role TEXT NOT NULL CHECK(sender_role IN ('user', 'agent')),
            sender_name TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket ON ticket_messages(ticket_id, id)"
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
    username: str = "",
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
        "username": username,
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
    """返回已存的全部工单，兼容缺少元数据的历史记录。"""
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


def list_user_tickets(user_id: str, limit: int = 20, offset: int = 0) -> dict:
    """返回指定用户自己的工单，新的在前（跨用户隔离）。"""
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT result_json FROM ticket_idempotency"
        ).fetchall()
        tickets = []
        for row in rows:
            ticket = json.loads(row[0])
            if ticket.get("user_id") == user_id:
                tickets.append(ticket)
        tickets.sort(key=lambda ticket: ticket.get("created_at", ""), reverse=True)
        return {
            "tickets": tickets[offset : offset + limit],
            "total": len(tickets),
        }
    finally:
        connection.close()


def update_ticket_status(
    ticket_id: str,
    status: str,
    operator_id: str,
    operator: str,
) -> dict | None:
    """在工作台更新工单状态，用数据库写事务保证一致。"""
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


def get_ticket(ticket_id: str) -> dict | None:
    """按工单号取单条工单记录；不存在返回 None。"""
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT result_json FROM ticket_idempotency"
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        ticket = json.loads(row[0])
        if ticket.get("ticket_id") == ticket_id:
            return ticket
    return None


def add_ticket_message(
    ticket_id: str,
    sender_role: str,
    content: str,
    sender_name: str = "",
) -> dict | None:
    """向工单追加一条对话消息；工单不存在时返回 None。"""
    content = (content or "").strip()
    if not content or len(content) > 2000:
        raise ValueError("消息内容须为 1-2000 字")
    if sender_role not in {"user", "agent"}:
        raise ValueError("sender_role 只能是 user 或 agent")
    connection = _connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT result_json FROM ticket_idempotency"
        ).fetchall()
        found = False
        for row in rows:
            ticket = json.loads(row[0])
            if ticket.get("ticket_id") == ticket_id:
                found = True
                break
        if not found:
            connection.rollback()
            return None
        created_at = datetime.now(timezone.utc).isoformat()
        cursor = connection.execute(
            "INSERT INTO ticket_messages (ticket_id, sender_role, sender_name, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (ticket_id, sender_role, sender_name, content, created_at),
        )
        connection.commit()
        return {
            "id": cursor.lastrowid,
            "ticket_id": ticket_id,
            "sender_role": sender_role,
            "sender_name": sender_name,
            "content": content,
            "created_at": created_at,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_ticket_messages(ticket_id: str, limit: int = 100) -> list[dict]:
    """按时间正序返回工单对话（默认最近 100 条）。"""
    connection = _connect()
    try:
        rows = connection.execute(
            "SELECT id, sender_role, sender_name, content, created_at FROM ticket_messages WHERE ticket_id = ? ORDER BY id DESC LIMIT ?",
            (ticket_id, limit),
        ).fetchall()
        return [
            {
                "id": row[0],
                "ticket_id": ticket_id,
                "sender_role": row[1],
                "sender_name": row[2],
                "content": row[3],
                "created_at": row[4],
            }
            for row in reversed(rows)
        ]
    finally:
        connection.close()

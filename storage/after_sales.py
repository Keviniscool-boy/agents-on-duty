"""Demo after-sales service; no payments are executed."""

import hashlib
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "after_sales.sqlite3"


def _connect():
    db_path = Path(os.getenv("AFTER_SALES_DB_PATH") or DEFAULT_DB_PATH)
    if not db_path.is_absolute():
        db_path = DEFAULT_DB_PATH.parents[1] / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('delivered', 'cancelled', 'paid', 'shipped')),
            amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0), delivered_at TEXT, product TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS refund_requests (
            id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
            user_id TEXT NOT NULL, reason TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending_review', 'cancelled', 'approved', 'rejected', 'completed')),
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS refund_tickets (
            id TEXT PRIMARY KEY, refund_id TEXT NOT NULL UNIQUE REFERENCES refund_requests(id),
            user_id TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending', 'processing', 'cancelled', 'resolved', 'rejected')), created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_refund_per_order
            ON refund_requests(order_id) WHERE status = 'pending_review';
        CREATE TABLE IF NOT EXISTS workbench_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            refund_id TEXT NOT NULL,
            operator_id TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('approve', 'reject', 'need_info')),
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS refund_idempotency (
            user_id TEXT NOT NULL, request_key TEXT NOT NULL,
            order_id TEXT NOT NULL, reason TEXT NOT NULL,
            refund_id TEXT NOT NULL REFERENCES refund_requests(id),
            PRIMARY KEY(user_id, request_key)
        );
    """)
    # 阶段二迁移：存量库补 session_id 列（记录发起会话，供工作台恢复线程）。
    try:
        db.execute("ALTER TABLE refund_requests ADD COLUMN session_id TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    return db


def _now():
    return datetime.now(timezone.utc)


def list_orders(user_id: str) -> list[dict]:
    """Provision two sample orders for each account once."""
    if not user_id:
        raise ValueError("user_id is required")
    prefix = hashlib.sha256(user_id.encode()).hexdigest()[:24].upper()
    with closing(_connect()) as db, db:
        for suffix, status, delivered in (("1", "delivered", (_now() - timedelta(days=1)).isoformat()),
                                           ("2", "cancelled", None)):
            db.execute("INSERT OR IGNORE INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                       (f"DEMO-{prefix}-{suffix}", user_id, status, 29900, delivered, "演示商品"))
        rows = db.execute("SELECT id AS order_id, status, amount_cents, delivered_at, product FROM orders WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
    return [{**dict(row), "mock": True} for row in rows]


def get_order(user_id: str, order_id: str) -> dict:
    with closing(_connect()) as db:
        row = db.execute("SELECT id AS order_id, status, amount_cents, delivered_at, product FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id)).fetchone()
    return {**dict(row), "mock": True} if row else {"error": "order_not_found", "message": "订单不存在或无权访问"}


def apply_refund(user_id: str, order_id: str, reason: str, idempotency_key: str, session_id: str = "") -> dict:
    reason = reason.strip()
    idempotency_key = idempotency_key.strip()
    if not reason or len(reason) > 500:
        return {"error": "invalid_reason", "message": "请填写不超过 500 字的退款原因"}
    if not idempotency_key or len(idempotency_key) > 128:
        return {"error": "invalid_idempotency_key", "message": "请提供不超过 128 字的幂等键"}
    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        order = db.execute("SELECT * FROM orders WHERE id = ? AND user_id = ?", (order_id, user_id)).fetchone()
        if not order:
            return {"error": "order_not_found", "message": "订单不存在或无权访问"}
        previous = db.execute(
            "SELECT order_id, reason, refund_id FROM refund_idempotency WHERE user_id = ? AND request_key = ?",
            (user_id, idempotency_key),
        ).fetchone()
        if previous:
            if previous["order_id"] != order_id or previous["reason"] != reason:
                return {"error": "idempotency_conflict", "message": "该幂等键已用于其他请求"}
            saved = db.execute(
                "SELECT r.id, r.order_id, r.reason, r.status, r.created_at, t.id AS ticket_id FROM refund_requests r LEFT JOIN refund_tickets t ON t.refund_id = r.id WHERE r.id = ? AND r.user_id = ?",
                (previous["refund_id"], user_id),
            ).fetchone()
            return {**dict(saved), "reused": True, "mock": True}
        existing = db.execute("SELECT r.id, r.order_id, r.reason, r.status, r.created_at, t.id AS ticket_id FROM refund_requests r LEFT JOIN refund_tickets t ON t.refund_id = r.id WHERE r.order_id = ? AND r.user_id = ? AND r.status = 'pending_review'", (order_id, user_id)).fetchone()
        if existing:
            if existing["reason"] != reason:
                return {"error": "refund_exists", "message": "该订单已有退款申请，请查看原申请"}
            db.execute("INSERT INTO refund_idempotency VALUES (?, ?, ?, ?, ?)",
                       (user_id, idempotency_key, order_id, reason, existing["id"]))
            db.commit()
            return {**dict(existing), "reused": True, "mock": True}
        if order["status"] != "delivered" or not order["delivered_at"]:
            return {"error": "not_eligible", "message": "演示规则：只有已签收订单可以申请退款"}
        elapsed = _now() - datetime.fromisoformat(order["delivered_at"])
        if elapsed < timedelta(0):
            return {"error": "not_eligible", "message": "演示订单签收时间无效，请联系人工客服"}
        if elapsed > timedelta(days=7):
            return {"error": "not_eligible", "message": "演示规则：签收超过 7 天不能申请退款"}
        refund_id = "RF-" + uuid.uuid4().hex.upper()
        created = _now().isoformat()
        db.execute("INSERT INTO refund_requests VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (refund_id, order_id, user_id, reason, "pending_review", created, session_id))
        ticket_id = "TK-" + uuid.uuid4().hex.upper()
        db.execute("INSERT INTO refund_tickets VALUES (?, ?, ?, ?, ?)",
                   (ticket_id, refund_id, user_id, "pending", created))
        db.execute("INSERT INTO refund_idempotency VALUES (?, ?, ?, ?, ?)",
                   (user_id, idempotency_key, order_id, reason, refund_id))
        db.commit()
        return {"id": refund_id, "order_id": order_id, "reason": reason,
                "ticket_id": ticket_id, "status": "pending_review", "created_at": created, "reused": False, "mock": True}
    finally:
        db.close()


def list_refunds(user_id: str) -> list[dict]:
    with closing(_connect()) as db:
        rows = db.execute("SELECT r.id, r.order_id, r.reason, r.status, r.session_id, r.created_at, t.id AS ticket_id FROM refund_requests r LEFT JOIN refund_tickets t ON t.refund_id = r.id WHERE r.user_id = ? ORDER BY r.created_at DESC", (user_id,)).fetchall()
    return [{**dict(row), "mock": True} for row in rows]


def cancel_refund(user_id: str, refund_id: str) -> dict:
    """Cancel a pending request and its ticket atomically; repeated cancellation is safe."""
    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        refund = db.execute(
            "SELECT id, order_id, reason, status, created_at FROM refund_requests WHERE id = ? AND user_id = ?",
            (refund_id, user_id),
        ).fetchone()
        if not refund:
            return {"error": "refund_not_found", "message": "退款申请不存在或无权访问"}
        if refund["status"] == "cancelled":
            return {**dict(refund), "reused": True, "mock": True}
        if refund["status"] != "pending_review":
            return {"error": "invalid_state", "message": "当前状态无法撤回退款申请"}
        updated = db.execute(
            "UPDATE refund_requests SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'pending_review'",
            (refund_id, user_id),
        )
        ticket = db.execute(
            "UPDATE refund_tickets SET status = 'cancelled' WHERE refund_id = ? AND user_id = ? AND status = 'pending'",
            (refund_id, user_id),
        )
        if updated.rowcount != 1 or ticket.rowcount != 1:
            raise RuntimeError("Refund and ticket state are inconsistent")
        db.commit()
        return {**dict(refund), "status": "cancelled", "reused": False, "mock": True}
    finally:
        db.close()


def _enrich_usernames(rows: list[dict], id_field: str, target_field: str) -> list[dict]:
    """售后库与账户库是两个 SQLite 文件，用户名在 Python 层回填。"""
    from auth.database import get_usernames_by_ids

    usernames = get_usernames_by_ids([row.get(id_field, "") for row in rows])
    for row in rows:
        row[target_field] = usernames.get(row.get(id_field, ""))
    return rows


def list_refund_reviews() -> list[dict]:
    """客服工作台：全部退款申请（含会话关联），按时间倒序。"""
    with closing(_connect()) as db:
        rows = db.execute(
            """
            SELECT r.id, r.order_id, r.user_id, r.reason, r.status,
                   r.session_id, r.created_at, t.id AS ticket_id, t.status AS ticket_status,
                   o.status AS order_status, o.amount_cents, o.product
            FROM refund_requests r
            LEFT JOIN refund_tickets t ON t.refund_id = r.id
            LEFT JOIN orders o ON o.id = r.order_id
            ORDER BY r.created_at DESC
            """
        ).fetchall()
    return _enrich_usernames([{**dict(row), "mock": True} for row in rows], "user_id", "username")


def get_refund_review(refund_id: str) -> dict | None:
    """客服工作台：单个退款申请详情（含订单与关联工单）。"""
    with closing(_connect()) as db:
        row = db.execute(
            """
            SELECT r.id, r.order_id, r.user_id, r.reason, r.status,
                   r.session_id, r.created_at, t.id AS ticket_id, t.status AS ticket_status,
                   o.status AS order_status, o.amount_cents, o.product
            FROM refund_requests r
            LEFT JOIN refund_tickets t ON t.refund_id = r.id
            LEFT JOIN orders o ON o.id = r.order_id
            WHERE r.id = ?
            """,
            (refund_id,),
        ).fetchone()
    if row is None:
        return None
    return _enrich_usernames([{**dict(row), "mock": True}], "user_id", "username")[0]


def list_refund_actions(refund_id: str) -> list[dict]:
    """某个退款申请的人工操作审计记录。"""
    with closing(_connect()) as db:
        rows = db.execute(
            """
            SELECT a.id, a.refund_id, a.operator_id,
                   a.action, a.note, a.created_at
            FROM workbench_actions a
            WHERE a.refund_id = ?
            ORDER BY a.id
            """,
            (refund_id,),
        ).fetchall()
    return _enrich_usernames([dict(row) for row in rows], "operator_id", "operator")


def record_refund_decision(refund_id: str, action: str, note: str, operator_id: str) -> dict:
    """人工决定：批准 / 拒绝 / 需补充信息，联动工单状态并写审计。

    事务内完成退款单与工单流转；need_info 保持 pending_review
    （工单转 processing 并记录追问内容），approve/reject 终结两者。
    返回 {error: ...} 或更新后的申请信息。
    """
    transitions = {
        "approve": ("approved", "resolved"),
        "reject": ("rejected", "rejected"),
        "need_info": ("pending_review", "processing"),
    }
    if action not in transitions:
        return {"error": "invalid_action", "message": "不支持的操作类型"}
    new_refund_status, new_ticket_status = transitions[action]

    db = _connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        refund = db.execute(
            "SELECT id, order_id, reason, status, created_at FROM refund_requests WHERE id = ?",
            (refund_id,),
        ).fetchone()
        if not refund:
            return {"error": "refund_not_found", "message": "退款申请不存在"}
        if refund["status"] != "pending_review":
            return {"error": "invalid_state", "message": f"当前状态（{refund['status']}）无法审批"}

        if action == "need_info":
            db.execute(
                "INSERT INTO workbench_actions (refund_id, operator_id, action, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (refund_id, operator_id, action, note.strip()[:500], _now().isoformat()),
            )
            ticket = db.execute(
                "UPDATE refund_tickets SET status = 'processing' WHERE refund_id = ? AND status = 'pending'",
                (refund_id,),
            )
            if ticket.rowcount != 1:
                raise RuntimeError("Refund and ticket state are inconsistent")
            db.commit()
            return {**dict(refund), "status": "pending_review", "reused": False, "mock": True}

        updated = db.execute(
            "UPDATE refund_requests SET status = ? WHERE id = ? AND status = 'pending_review'",
            (new_refund_status, refund_id),
        )
        ticket = db.execute(
            "UPDATE refund_tickets SET status = ? WHERE refund_id = ? AND status = 'pending'",
            (new_ticket_status, refund_id),
        )
        if updated.rowcount != 1 or ticket.rowcount != 1:
            raise RuntimeError("Refund and ticket state are inconsistent")
        db.execute(
            "INSERT INTO workbench_actions (refund_id, operator_id, action, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (refund_id, operator_id, action, note.strip()[:500], _now().isoformat()),
        )
        db.commit()
        return {**dict(refund), "status": new_refund_status, "reused": False, "mock": True}
    finally:
        db.close()

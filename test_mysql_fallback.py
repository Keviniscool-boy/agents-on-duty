"""MySQL 兜底存储与降级链的测试。

需要真实 MySQL 时会自动探测：连不上就跳过 MySQL 相关用例，
只验证降级链行为。
"""

import asyncio

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def _mysql_available() -> bool:
    try:
        from storage.mysql_store import mysql_store

        mysql_store.self_check()
        return True
    except Exception:
        return False


def test_mysql_self_check_fails_fast_without_server():
    """MySQL 连不上时 self_check 抛错（启动会被拒绝）。"""
    from storage.mysql_store import MySQLStore, MySQLUnavailableError

    store = MySQLStore()
    import os

    os.environ["MYSQL_PORT"] = "3399"  # 不可达端口
    try:
        try:
            store.self_check()
        except MySQLUnavailableError:
            pass
        else:
            raise AssertionError("MySQL 不可达时应抛 MySQLUnavailableError")
    finally:
        os.environ.pop("MYSQL_PORT", None)


def test_working_memory_degrades_to_in_process_when_all_backends_down():
    """Redis 与 MySQL 都不可用时，working memory 仍可工作。"""
    import os

    os.environ["MYSQL_HOST"] = "127.0.0.1"
    os.environ["MYSQL_PORT"] = "3399"
    try:
        from memory.working_memory import WorkingMemory

        memory = WorkingMemory(redis_url="redis://127.0.0.1:6399/0")
        memory.update("sess-degraded", {"last_intent": "ticket_handler"}, user_id="u1")
        context = memory.get_context("sess-degraded")
        assert context["last_intent"] == "ticket_handler"
        history = memory.get_history("sess-degraded")
        assert len(history) >= 1
        memory.clear("sess-degraded")
        assert memory.get_context("sess-degraded") == {}
    finally:
        os.environ.pop("MYSQL_HOST", None)
        os.environ.pop("MYSQL_PORT", None)


def test_short_term_degrades_to_in_process_when_all_backends_down():
    """Redis 与 MySQL 都不可用时，短期记忆仍可工作。"""
    import os

    os.environ["MYSQL_HOST"] = "127.0.0.1"
    os.environ["MYSQL_PORT"] = "3399"
    try:
        from memory.short_term import ShortTermMemory

        memory = ShortTermMemory(redis_url="redis://127.0.0.1:6399/0")
        memory.redis = None

        async def run():
            await memory.add_message("sess-st", "user", "你好", user_id="u1")
            history = await memory.get_history("sess-st")
            assert history[-1]["content"] == "你好"
            await memory.clear_history("sess-st")
            assert (await memory.get_history("sess-st")) == []

        asyncio.run(run())
    finally:
        os.environ.pop("MYSQL_HOST", None)
        os.environ.pop("MYSQL_PORT", None)


def test_mysql_store_roundtrip():
    """连得上 MySQL 时验证完整读写；连不上显式 skip（B9：不得静默计 passed）。"""
    if not _mysql_available():
        pytest.skip("MySQL unreachable - roundtrip not executed (see CI mysql service)")

    import uuid

    from storage.mysql_store import mysql_store

    session_id = f"test-{uuid.uuid4().hex[:12]}"
    try:
        mysql_store.save_working_context(
            session_id, "user-1", {"last_intent": "knowledge_rag"}
        )
        mysql_store.save_working_context(
            session_id, "user-1", {"current_agent": "knowledge_rag"}
        )
        context = mysql_store.load_working_context(session_id)
        assert context["last_intent"] == "knowledge_rag"
        assert context["current_agent"] == "knowledge_rag"

        mysql_store.append_working_history(
            session_id,
            "user-1",
            {"timestamp": "2026-01-01T00:00:00+00:00", "data": {"a": 1}},
            max_entries=2,
        )
        mysql_store.append_working_history(
            session_id,
            "user-1",
            {"timestamp": "2026-01-01T00:00:01+00:00", "data": {"b": 2}},
            max_entries=2,
        )
        history = mysql_store.load_working_history(session_id)
        assert len(history) == 2

        mysql_store.add_message(
            session_id, "user-1", {"role": "user", "content": "查订单"}
        )
        mysql_store.add_message(
            session_id, "user-1", {"role": "assistant", "content": "好的"}
        )
        messages = mysql_store.get_messages(session_id, 10)
        assert [m["role"] for m in messages] == ["user", "assistant"]

        mysql_store.clear_working(session_id)
        assert mysql_store.load_working_context(session_id) == {}
        assert mysql_store.load_working_history(session_id) == []
    finally:
        mysql_store.clear_messages(session_id)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                if type(exc).__name__ == "Skipped":
                    print(f"SKIP {name}: {exc}")
                    continue
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print("ALL TESTS PASSED" if failures == 0 else f"{failures} FAILURE(S)")
    sys.exit(1 if failures else 0)

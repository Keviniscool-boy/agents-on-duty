"""A6/A7 多副本并发测试：多线程原子合并同一会话不丢更新。

需要真实 MySQL；不可达时显式 skip（CI 有真实 MySQL 门禁）。
"""

import sys
import threading
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from storage.mysql_store import MySQLStore


def _mysql_available() -> bool:
    try:
        MySQLStore().self_check()
        return True
    except Exception:
        return False


def test_a67_concurrent_merge_no_lost_update():
    """8 个独立连接并发合并同一会话的不同字段，最终全部可见。"""
    if not _mysql_available():
        pytest.skip("MySQL unreachable - concurrency test needs real MySQL")

    session_id = f"conc-{uuid.uuid4().hex[:12]}"
    workers = 8
    store = MySQLStore()
    store.self_check()
    errors = []

    def worker(i: int):
        try:
            # 每线程独立连接（独立实例），真实触发 MySQL 行锁竞争。
            s = MySQLStore()
            s.merge_working_context(session_id, f"user-{i}", {f"key_{i}": i})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"并发合并出现异常: {errors}"
    final = store.load_working_context(session_id)
    missing = [f"key_{i}" for i in range(workers) if f"key_{i}" not in final]
    assert not missing, f"丢失更新：{missing} 未出现在最终上下文（丢失更新回归）"

    store.clear_working(session_id)

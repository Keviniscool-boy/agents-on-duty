"""兜底链路修复行为的独立回归测试（对应审查报告 A1-A4、B1、B14）。

语义说明：verify_fallback.py（外部脚本）的 PASS 表示"缺陷被复现"；
修复后那些用例会转 FAIL。本文件是修复行为的**正向**断言——全部
PASS 才是健康状态。全部使用确定性替身，不需要真实 MySQL/Redis。
"""

import sys
from pathlib import Path
from unittest import mock

import pymysql
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from auth import security as security_module
from memory import short_term as st_mod
from memory import working_memory as wm_mod
from memory.long_term import LongTermMemory
from storage.mysql_store import MySQLStore, MySQLUnavailableError


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.ops = []

    def hset(self, key, mapping=None):
        self.ops.append(("hset", key, mapping))

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))

    def rpush(self, key, value):
        self.ops.append(("rpush", key, value))

    def ltrim(self, key, start, end):
        self.ops.append(("ltrim", key, start, end))

    def execute(self):
        for op in self.ops:
            kind, key = op[0], op[1]
            if kind == "hset":
                self.client.hash.setdefault(key, {}).update(op[2])
            elif kind == "rpush":
                self.client.lists.setdefault(key, []).append(op[2])
        return self.ops


class FakeRedis:
    instances = 0

    def __init__(self):
        self.hash = {}
        self.lists = {}

    @classmethod
    def from_url(cls, url, **kwargs):
        cls.instances += 1
        return cls()

    def ping(self):
        return True

    def pipeline(self):
        return FakePipeline(self)

    def hgetall(self, key):
        return self.hash.get(key, {})

    def lrange(self, key, start, end):
        return self.lists.get(key, [])

    def delete(self, *keys):
        for key in keys:
            self.hash.pop(key, None)
            self.lists.pop(key, None)


class FakeMySQLStore:
    """MySQLStore 替身：可切换可用性，记录调用。"""

    def __init__(self):
        self.available = True
        self.calls = []
        self.contexts = {}
        self.history = {}
        self.messages = {}

    def load_working_context(self, session_id):
        self.calls.append("load_working_context")
        self._fail()
        return dict(self.contexts.get(session_id, {}))

    def save_working_context(self, session_id, user_id, context):
        self.calls.append("save_working_context")
        self._fail()
        self.contexts[session_id] = dict(context)

    def append_working_history(self, session_id, user_id, entry, max_entries):
        self.calls.append("append_working_history")
        self._fail()
        self.history.setdefault(session_id, []).append(entry)

    def load_working_history(self, session_id):
        self.calls.append("load_working_history")
        self._fail()
        return list(self.history.get(session_id, []))

    def add_message(self, session_id, user_id, message):
        self.calls.append("add_message")
        self._fail()
        self.messages.setdefault(session_id, []).append(message)

    def get_messages(self, session_id, last_n):
        self.calls.append("get_messages")
        self._fail()
        return list(self.messages.get(session_id, []))[-last_n:]

    def _fail(self):
        if not self.available:
            raise MySQLUnavailableError("simulated MySQL down")


@pytest.fixture()
def fake_mysql(monkeypatch):
    store = FakeMySQLStore()
    monkeypatch.setattr(wm_mod, "mysql_store", store)
    monkeypatch.setattr(st_mod, "mysql_store", store)
    return store


@pytest.fixture()
def fake_redis_ok(monkeypatch):
    FakeRedis.instances = 0
    monkeypatch.setattr("redis.Redis.from_url", FakeRedis.from_url)
    return FakeRedis


# ---------------- A1：write-through 扇出 ----------------


def test_a1_write_through_reaches_mysql_when_redis_up(fake_mysql, fake_redis_ok):
    """A1-a 反转：Redis 在线写入时 MySQL 同步收到副本。"""
    memory = wm_mod.WorkingMemory(redis_url="redis://fake:6379/0")
    memory.update("sess", {"last_intent": "ticket_handler"}, user_id="u1")

    assert any(c.startswith("save") for c in fake_mysql.calls), (
        f"MySQL 应收到扇出写入，实际调用={fake_mysql.calls}"
    )


def test_a1_redis_down_still_reads_data(fake_mysql, fake_redis_ok, monkeypatch):
    """A1-b 反转：Redis 故障后读 MySQL 仍能看到此前写入。"""
    memory = wm_mod.WorkingMemory(redis_url="redis://fake:6379/0")
    memory.update("sess", {"last_intent": "ticket_handler"}, user_id="u1")

    # 模拟 Redis 进程内失效；MySQL 层持有完整副本
    memory._redis = None
    monkeypatch.setattr(
        "redis.Redis.from_url",
        lambda url, **kw: (_ for _ in ()).throw(ConnectionError("redis down")),
    )
    context = memory.get_context("sess")
    assert context.get("last_intent") == "ticket_handler"


# ---------------- A2：SQL 执行期异常入兜底 ----------------


def test_a2_runtime_sql_error_converted_to_unavailable():
    """A2 反转：连接建立后的 SQL 执行错误转为 MySQLUnavailableError。"""
    store = MySQLStore()
    store._conn = mock.Mock()
    store._conn.cursor.return_value.execute.side_effect = (
        pymysql.err.OperationalError(1213, "Deadlock found")
    )
    with pytest.raises(MySQLUnavailableError):
        store.load_working_context("sess")


@pytest.mark.anyio
async def test_a2_sql_error_falls_back_to_in_process(fake_mysql, monkeypatch):
    """A2 反转：执行期死锁不再穿透，消息落进程内兜底。"""

    def add_message_raises(session_id, user_id, message):
        fake_mysql.calls.append("add_message")
        raise pymysql.err.OperationalError(1213, "Deadlock found")

    fake_mysql.add_message = add_message_raises

    # aioredis.from_url 本身是同步工厂函数，替身也应是同步 raise。
    def boom_from_url(url, **kwargs):
        raise ConnectionError("redis down")

    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis, "from_url", boom_from_url)
    memory = st_mod.ShortTermMemory(redis_url="redis://fake:6379/0")

    await memory.add_message("sess-a2", "user", "你好", user_id="u1")
    assert memory._fallback_store.get("sess-a2"), "消息应落入进程内兜底"


# ---------------- A3：恢复后数据仍可见 ----------------


def test_a3_outage_data_visible_after_recovery(fake_mysql):
    """A3 反转：双后端故障期写入进程内，恢复后仍可读。"""
    fake_mysql.available = False
    memory = wm_mod.WorkingMemory(redis_url=None)
    memory.update("sess-a3", {"last_intent": "knowledge_rag"}, user_id="u1")
    assert memory.get_context("sess-a3").get("last_intent") == "knowledge_rag"

    fake_mysql.available = True  # 后端恢复
    context = memory.get_context("sess-a3")
    assert context.get("last_intent") == "knowledge_rag", (
        f"恢复后应仍能读到故障期数据，实际={context}"
    )


# ---------------- A4：回切 ----------------


def test_a4_redis_failover_after_recovery(fake_mysql, monkeypatch):
    """A4 反转：Redis 瞬时故障被标记，恢复后下一次操作立即重探并回切。"""
    calls = {"n": 0}

    def flaky_from_url(url, **kwargs):
        calls["n"] += 1
        raise ConnectionError("transient redis failure")

    monkeypatch.setattr("redis.Redis.from_url", flaky_from_url)
    memory = wm_mod.WorkingMemory(redis_url="redis://real:6379/0")
    memory.update("sess-a4", {"a": 1}, user_id="u1")
    assert calls["n"] == 1 and memory._redis is None and memory._redis_failures == 1

    # Redis 恢复：换成可用实现，下一次操作必须重探
    FakeRedis.instances = 0
    monkeypatch.setattr("redis.Redis.from_url", FakeRedis.from_url)
    memory.update("sess-a4", {"b": 2}, user_id="u1")

    assert FakeRedis.instances == 1, "恢复后应重新探测 Redis"
    assert memory._redis is not None and memory._redis_failures == 0, "应回切到 Redis"


# ---------------- B1：JWT 密钥必填 ----------------


def test_b1_jwt_secret_required(monkeypatch):
    """B1：未配置或历史默认密钥时拒绝签发，不再静默回退。"""
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        security_module.create_access_token("u1", "alice")

    monkeypatch.setenv("JWT_SECRET_KEY", "change-this-development-secret")
    with pytest.raises(RuntimeError):
        security_module.create_access_token("u1", "alice")

    monkeypatch.setenv("JWT_SECRET_KEY", "x" * 32)
    token = security_module.create_access_token("u1", "alice")
    assert isinstance(token, str) and token


# ---------------- B14：降级检索同阈值同分数语义 ----------------


def test_b14_fallback_search_applies_min_score(monkeypatch):
    """B14：降级路径分数归一化 0~1 并应用 min_score。"""
    memory = LongTermMemory.__new__(LongTermMemory)
    memory.index = None
    memory.documents = [
        {"id": "a", "content": "退货政策：七天无理由退货", "source": "a.txt", "metadata": {}},
        {"id": "b", "content": "开户流程说明", "source": "b.txt", "metadata": {}},
    ]
    monkeypatch.setattr(memory, "embed_text", mock.AsyncMock())

    results = memory._fallback_search("退货政策 七天无理由退货", top_k=5, min_score=0.6)
    assert [r["id"] for r in results] == ["a"]
    assert 0.0 <= results[0]["score"] <= 1.0

    # 无关查询不得通过降级路径混入（旧实现会返回所有 word-match 文档）
    assert memory._fallback_search("完全不相关的问题", top_k=5, min_score=0.6) == []

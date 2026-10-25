"""Working memory 的 pytest 用例：进程内降级模式下的读写与清理。"""

from memory.working_memory import WorkingMemory


def test_update_and_get_context():
    memory = WorkingMemory(redis_url=None)
    memory.update("session-001", {"last_intent": "knowledge_rag"})
    memory.update("session-001", {"current_agent": "knowledge_rag"})

    context = memory.get_context("session-001")
    assert context["last_intent"] == "knowledge_rag"
    assert context["current_agent"] == "knowledge_rag"


def test_history_keeps_most_recent_entries():
    memory = WorkingMemory(max_entries=2)
    for round_index in range(4):
        memory.update("session-001", {"round": round_index})

    history = memory.get_history("session-001")
    assert [item["data"]["round"] for item in history] == [2, 3]


def test_clear_removes_session():
    memory = WorkingMemory(redis_url=None)
    memory.update("session-001", {"last_intent": "ticket_handler"})
    memory.clear("session-001")

    assert memory.get_context("session-001") == {}
    assert memory.get_history("session-001") == []


def test_sessions_are_isolated():
    memory = WorkingMemory(redis_url=None)
    memory.update("session-a", {"agent": "knowledge_rag"})
    memory.update("session-b", {"agent": "ticket_handler"})

    assert memory.get_context("session-a")["agent"] == "knowledge_rag"
    assert memory.get_context("session-b")["agent"] == "ticket_handler"

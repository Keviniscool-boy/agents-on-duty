"""知识库 embedding 降级链路的回归测试。

覆盖：异常包装闭合（底层任意异常 → EmbeddingUnavailable）、
建库半途失败的回滚与磁盘零污染、粘性降级语义、
B14 关键词兜底检索、knowledge_status 状态迁移、
以及 knowledge_rag 对 EmbeddingUnavailable 的优雅拒答。
"""

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agents.knowledge_rag import KnowledgeRAGAgent
from memory.long_term import EmbeddingUnavailable, LongTermMemory


class FlakyEmbeddings:
    """可编程的假 embedding：按脚本决定抛错或返回向量。"""

    def __init__(self, script):
        # script: 每次调用弹出一项；True=返回向量，Exception 实例=抛出。
        self.script = list(script)

    async def aembed_query(self, text: str) -> list[float]:
        step = self.script.pop(0) if self.script else self.script[-1]
        if isinstance(step, Exception):
            raise step
        return [0.5, 0.5]


@pytest.fixture
def ltm(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dummy-for-tests")
    memory = LongTermMemory(index_path=str(tmp_path / "faiss_index"))
    return memory


def _wire(memory, script):
    memory.embeddings = FlakyEmbeddings(script)
    return memory


def test_embed_text_wraps_any_provider_failure(ltm):
    """底层任意异常（HTTP 403/超时/构造失败）都统一包装成 EmbeddingUnavailable。"""
    _wire(ltm, [RuntimeError("connection reset"), TimeoutError(), Exception("boom")])
    for _ in range(3):
        with pytest.raises(EmbeddingUnavailable):
            import asyncio

            asyncio.run(ltm.embed_text("问题"))
    assert ltm.embedding_available is False
    assert ltm.knowledge_status() == "degraded"


def test_build_failure_rolls_back_and_keeps_disk_clean(ltm, tmp_path):
    """第 N 块失败：还原入口时的索引与文档快照，不落盘任何文件。"""
    _wire(ltm, [True, RuntimeError("quota exhausted")])  # 第一块成功，第二块失败
    kb = tmp_path / "kb"
    kb.mkdir()
    # 第二段超过 512 字符，确保切成两个独立 chunk（小段落会被合并）。
    long_paragraph = "超长政策条款" * 100
    (kb / "a.txt").write_text(f"第一段内容\n\n{long_paragraph}", encoding="utf-8")

    count = asyncio.run(ltm.load_knowledge_base(str(kb)))
    assert count == 0
    assert ltm.index_build_failed is True
    assert ltm.documents == [] and ltm.index is None, "半成品必须整体回滚"
    assert list(tmp_path.glob("faiss_index*")) == [], "失败路径不得写盘"
    assert ltm.knowledge_status() == "degraded"


def test_build_failure_with_previous_index_retains_it(ltm, tmp_path):
    """已有索引时建库失败：保留旧索引旧文档，粘性降级拒绝检索。"""
    _wire(ltm, [True, True])
    import asyncio

    asyncio.run(ltm.add_document("退货政策：七天无理由", source="policy"))
    assert ltm.knowledge_status() == "ready"

    _wire(ltm, [RuntimeError("quota exhausted")])
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "new.txt").write_text("新增政策", encoding="utf-8")
    assert asyncio.run(ltm.load_knowledge_base(str(kb))) == 0

    assert ltm.index_build_failed is True
    assert len(ltm.documents) == 1, "旧文档保留"
    assert ltm.index is not None and ltm.index.ntotal == 1, "旧索引保留"
    with pytest.raises(EmbeddingUnavailable):
        import asyncio

        asyncio.run(ltm.search("退货"))
    assert ltm.knowledge_status() == "degraded"


def test_successful_build_saves_and_recovers_status(ltm, tmp_path):
    """凭据恢复后重建：落盘、状态回到 ready。"""
    _wire(ltm, [True, True, True])
    import asyncio

    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "a.txt").write_text("退款政策说明", encoding="utf-8")
    assert asyncio.run(ltm.load_knowledge_base(str(kb))) == 1
    assert ltm.index_build_failed is False
    assert ltm.knowledge_status() == "ready"
    assert (tmp_path / "faiss_index").exists(), "成功路径必须落盘"


def test_search_with_dead_embedding_at_query_time_raises_unavailable(ltm, tmp_path):
    """索引就绪但查询时 embedding 挂了：同样抛 EmbeddingUnavailable。"""
    _wire(ltm, [True])
    import asyncio

    asyncio.run(ltm.add_document("退款政策说明", source="policy"))
    _wire(ltm, [RuntimeError("provider down")])
    with pytest.raises(EmbeddingUnavailable):
        asyncio.run(ltm.search("退款"))


def test_fallback_search_works_when_index_is_none(ltm):
    """索引缺失但文档在（损坏/身份变更路径）：B14 关键词兜底仍然可用。"""
    ltm.index = None
    ltm.documents = [{"id": "x", "content": "退款需要在七天内申请", "source": "s", "metadata": {}}]
    import asyncio

    results = asyncio.run(ltm.search("退款 七天", min_score=0.5))
    assert len(results) == 1 and results[0]["source"] == "s"


def test_knowledge_status_precedence(ltm):
    """状态优先级：degraded > empty > unknown > ready（代码语义：empty 先于 unknown）。"""
    assert ltm.knowledge_status() == "empty", "空库无论是否探测过都报 empty"
    ltm.index_build_failed = True
    assert ltm.knowledge_status() == "degraded", "失败优先于一切"
    ltm.index_build_failed = False
    # 有文档但从未探测过 embedding → unknown
    ltm.documents = [{"id": "x", "content": "内容", "source": "s", "metadata": {}}]
    assert ltm.knowledge_status() == "unknown"
    _wire(ltm, [True])
    asyncio.run(ltm.add_document("新增内容", source="s"))
    assert ltm.knowledge_status() == "ready"


def test_knowledge_rag_answers_gracefully_on_unavailable():
    """Agent 级：检索抛 EmbeddingUnavailable 时答复固定降级话术，不炸图。"""

    class UnavailableMemory:
        async def search(self, *args, **kwargs):
            raise EmbeddingUnavailable("Embedding service unavailable")

    agent = KnowledgeRAGAgent(
        llm=FakeReplyLLM([AIMessage(content="退货政策")]),
        long_term_memory=UnavailableMemory(),
    )
    result = asyncio.run(
        agent.process(
            {"messages": [HumanMessage(content="退货政策是什么")], "sub_results": {}}
        )
    )
    assert "暂时不可用" in result["final_response"]
    assert result["sub_results"]["knowledge_error"] == "embedding_unavailable"


class FakeReplyLLM:
    def __init__(self, responses):
        self.responses = list(responses)

    async def ainvoke(self, messages, **kwargs):
        return self.responses.pop(0)

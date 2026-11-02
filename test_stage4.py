"""Stage 4 tests: hybrid retrieval, refusal/handoff, policy metadata, metrics."""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from agents.knowledge_rag import KnowledgeRAGAgent
from evaluation.metrics import score_case
from memory.long_term import LongTermMemory
from storage import after_sales_policy


class KeywordishEmbeddings:
    """Deterministic fake embedding: 'alpha' is the only vector-close token."""

    async def aembed_query(self, text: str) -> list[float]:
        return [1.0, 0.0] if "alpha" in text else [0.0, 1.0]


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)

    async def ainvoke(self, messages, **kwargs):
        return AIMessage(content=self.replies.pop(0))


class EmptyMemory:
    async def search(self, *args, **kwargs):
        return []


class LowScoreMemory:
    async def search(self, *args, **kwargs):
        return [{"source": "POL-X v1 §a", "content": "weak", "score": 0.2}]


class GoodMemory:
    async def search(self, *args, **kwargs):
        return [{"source": "POL-X v1 §a", "content": "strong", "score": 0.9}]


def _policy(doc_id: str, version: str = "1.0", status: str = "published",
            body: str = "body", extra: str = "") -> str:
    return (
        "---\n"
        f"doc_id: {doc_id}\n"
        f'version: "{version}"\n'
        f"status: {status}\n"
        'effective_date: "2020-01-01"\n'
        "expiration_date: null\n"
        f"{extra}"
        "---\n"
        "# Policy\n## Clause\n"
        f"{body}\n"
    )


def test_hybrid_retrieval_adds_keyword_only_hit(tmp_path):
    memory = LongTermMemory(index_path=str(tmp_path / "index"))
    memory.embeddings = KeywordishEmbeddings()
    asyncio.run(memory.add_document("alpha beta policy", source="alpha", metadata={}))
    asyncio.run(memory.add_document("gamma beta policy", source="gamma", metadata={}))

    results = asyncio.run(memory.search("beta", top_k=5, min_score=0.55))
    sources = [result["source"] for result in results]
    assert "gamma" in sources, "向量命中必须保留"
    assert "alpha" in sources, "关键词命中应被融合进来（向量分数为 0）"
    assert any(result.get("retrieval") == "hybrid" for result in results)


def test_knowledge_rag_refuses_without_results():
    agent = KnowledgeRAGAgent(FakeLLM(["query"]), EmptyMemory())
    result = asyncio.run(agent.process({
        "messages": [HumanMessage(content="退款政策是什么")],
        "sub_results": {},
    }))
    assert result["sub_results"]["knowledge_decision"] == "refuse"
    assert result["sub_results"]["handoff_required"] is True
    assert "没有相关信息" in result["final_response"]


def test_knowledge_rag_refuses_low_confidence():
    agent = KnowledgeRAGAgent(FakeLLM(["query", "0"]), LowScoreMemory())
    result = asyncio.run(agent.process({
        "messages": [HumanMessage(content="会员权益是什么")],
        "sub_results": {},
    }))
    assert result["sub_results"]["knowledge_decision"] == "refuse"
    assert "没有足够" in result["final_response"]


def test_knowledge_rag_answers_with_citation():
    agent = KnowledgeRAGAgent(
        FakeLLM(["query", "0", "answer with POL-X v1 §a"]),
        GoodMemory(),
    )
    result = asyncio.run(agent.process({
        "messages": [HumanMessage(content="会员权益")],
        "sub_results": {},
    }))
    assert result["sub_results"]["knowledge_decision"] == "answer"
    assert result["sub_results"]["sources"] == ["POL-X v1 §a"]
    assert result["sub_results"]["knowledge_rag_context"][0]["content"] == "strong"


def test_policy_docs_have_full_metadata(tmp_path):
    memory = LongTermMemory(index_path=str(tmp_path / "index"))
    memory.embeddings = KeywordishEmbeddings()
    count = asyncio.run(memory.load_knowledge_base(str(Path(__file__).parent / "knowledge_base")))
    assert count > 0
    doc_ids = {document["metadata"].get("doc_id") for document in memory.documents}
    expected = {
        "POL-AFTER-MVP-001",
        "POL-AFTER-SPECIAL-001",
        "POL-MEMBER-001",
        "POL-LOGISTICS-001",
        "POL-PRODUCT-001",
        "POL-ACCOUNT-001",
        "POL-SERVICE-001",
    }
    assert expected <= doc_ids
    for document in memory.documents:
        metadata = document["metadata"]
        assert metadata.get("source")
        assert metadata.get("version")
        assert metadata.get("status") == "published"
        assert metadata.get("effective_date")
        assert metadata.get("scope") is not None


def test_superseded_version_is_skipped(tmp_path):
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "old.md").write_text(
        _policy("P1", version="1.0", body="old body", extra="superseded_by: P1-v2.0\n"),
        encoding="utf-8",
    )
    (kb / "new.md").write_text(_policy("P1", version="2.0", body="new body"), encoding="utf-8")
    memory = LongTermMemory(index_path=str(tmp_path / "index"))
    memory.embeddings = KeywordishEmbeddings()
    assert asyncio.run(memory.load_knowledge_base(str(kb))) == 1
    assert memory.documents[0]["source"].startswith("P1 v2.0")
    assert "old body" not in str(memory.documents)


def test_special_product_goes_to_manual_review():
    now = datetime.now(timezone.utc)
    decision = after_sales_policy.evaluate("no_reason", "special", now, now)
    assert decision.action == "manual_review"
    assert decision.policy_ref.startswith("POL-AFTER-SPECIAL-001")


def test_stage4_metrics_score_answer_refuse_handoff():
    answer = score_case(
        {"expected_source": "POL-A-001", "expected_action": "answer"},
        ["POL-A-001 v1 §x"],
        "根据 POL-A-001 v1 §x，答案是……",
        {},
    )
    assert answer == {
        "recall_at_k": True,
        "citation_correct": True,
        "refusal_correct": True,
        "handoff_correct": True,
    }

    refuse = score_case(
        {"expected_source": None, "expected_action": "refuse"},
        [],
        "当前知识库没有相关信息，暂时无法确认。",
        {},
    )
    assert refuse["recall_at_k"] and refuse["citation_correct"] and refuse["refusal_correct"]

    handoff = score_case(
        {"expected_source": None, "expected_action": "handoff"},
        [],
        "请联系人工客服。",
        {"handoff_required": True},
    )
    assert handoff["recall_at_k"] and handoff["citation_correct"] and handoff["handoff_correct"]

    wrong = score_case(
        {"expected_source": None, "expected_action": "refuse"},
        ["POL-X v1 §x"],
        "这里有答案",
        {},
    )
    assert wrong["recall_at_k"] is False and wrong["citation_correct"] is False


def test_finalize_chat_exposes_handoff_decision(monkeypatch):
    import api.main as api_main

    class Working:
        def update(self, *args, **kwargs):
            pass

    class ShortTerm:
        async def add_message(self, *args, **kwargs):
            pass

    monkeypatch.setattr(api_main, "working_memory", Working())
    monkeypatch.setattr(api_main, "short_term_memory", ShortTerm())
    result = {
        "final_response": "暂时无法确认。",
        "intent": "knowledge_rag",
        "current_agent": "knowledge_rag",
        "compliance_passed": True,
        "compliance_reason": "",
        "sub_results": {"knowledge_decision": "refuse", "handoff_required": True},
    }
    response = asyncio.run(api_main._finalize_chat("session", "user", "问题", result))
    assert response.knowledge_decision == "refuse"
    assert response.handoff_required is True
    assert "人工客服" in response.response

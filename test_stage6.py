"""Stage 6 tests: knowledge API/MCP adapter, Pi comparison, business retention."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth.security import get_current_user
from evaluation.pi_comparison import RunnerResult, SubprocessPiRunner, decide, evaluate_runners, summarize
from knowledge.service import (
    KnowledgeService,
    KnowledgeUnavailableError,
    initialize_knowledge_memory,
)
from memory.long_term import EmbeddingUnavailable

PROJECT_ROOT = Path(__file__).resolve().parent
PI_DIR = PROJECT_ROOT / "experiments" / "pi_knowledge_agent"


class FakeMemory:
    documents = [
        {"metadata": {"doc_id": "POL-X", "version": "1.0", "section": "A"}},
    ]

    async def search(self, query, top_k=5, min_score=0.5):
        return [{
            "source": "POL-X v1.0 §A",
            "content": "七天无理由退货片段",
            "score": 0.92,
            "retrieval": "hybrid",
            "metadata": {"doc_id": "POL-X", "version": "1.0", "section": "A"},
        }]

    def knowledge_status(self):
        return "ready"


class FakeRunner:
    def __init__(self, answer="", sources=None, latency_ms=10.0, cost_usd=None, cost_known=None, error=""):
        self.answer = answer
        self.sources = sources or []
        self.latency_ms = latency_ms
        self.cost_usd = cost_usd
        self.cost_known = (cost_usd is not None) if cost_known is None else cost_known
        self.error = error

    def run(self, question):
        return RunnerResult(
            answer=self.answer,
            sources=self.sources,
            latency_ms=self.latency_ms,
            cost_usd=self.cost_usd,
            cost_known=self.cost_known,
            error=self.error,
        )


def test_knowledge_service_maps_hits():
    service = KnowledgeService(FakeMemory())
    hits = asyncio.run(service.search("退货", top_k=3, min_score=0.5))
    assert len(hits) == 1
    hit = hits[0]
    assert hit.doc_id == "POL-X"
    assert hit.version == "1.0"
    assert hit.section == "A"
    assert hit.source == "POL-X v1.0 §A"
    assert hit.retrieval == "hybrid"
    assert service.status() == {"status": "ready", "documents": 1}


def test_knowledge_api_search(monkeypatch):
    import api.main as api_main

    monkeypatch.setattr(api_main, "long_term_memory", FakeMemory())
    api_main.app.dependency_overrides[get_current_user] = lambda: {
        "id": "u", "username": "u", "role": "user",
    }
    try:
        client = TestClient(api_main.app)
        response = client.post("/api/knowledge/search", json={"query": "退货", "top_k": 3})
        assert response.status_code == 200
        hits = response.json()["hits"]
        assert hits and hits[0]["doc_id"] == "POL-X"
        status = client.get("/api/knowledge/status")
        assert status.status_code == 200 and status.json()["status"] == "ready"
    finally:
        api_main.app.dependency_overrides.pop(get_current_user, None)


class FailingMemory:
    documents = []

    async def search(self, query, top_k=5, min_score=0.5):
        raise EmbeddingUnavailable("provider down")

    def knowledge_status(self):
        return "degraded"


def test_knowledge_api_unavailable_returns_503(monkeypatch):
    import api.main as api_main

    monkeypatch.setattr(api_main, "long_term_memory", FailingMemory())
    api_main.app.dependency_overrides[get_current_user] = lambda: {
        "id": "u", "username": "u", "role": "user",
    }
    try:
        client = TestClient(api_main.app)
        response = client.post("/api/knowledge/search", json={"query": "退货"})
        assert response.status_code == 503
        assert response.json()["detail"]["error"] == "knowledge_unavailable"
    finally:
        api_main.app.dependency_overrides.pop(get_current_user, None)


class KbMemory:
    def __init__(self, failed=False, documents=None):
        self.index_build_failed = failed
        self.documents = documents if documents is not None else [{"content": "x"}]
        self.calls = []

    async def load_knowledge_base(self, path):
        self.calls.append(path)
        return 1


def test_initialize_knowledge_memory_rebuilds_and_fails_closed():
    memory = KbMemory()
    assert asyncio.run(initialize_knowledge_memory(memory, "/kb")) == 1
    assert memory.calls == ["/kb"]
    with pytest.raises(KnowledgeUnavailableError):
        asyncio.run(initialize_knowledge_memory(KbMemory(failed=True)))
    with pytest.raises(KnowledgeUnavailableError):
        asyncio.run(
            initialize_knowledge_memory(KbMemory(documents=[]), require_documents=True)
        )


def test_knowledge_mcp_tool(monkeypatch):
    import mcp_tools.knowledge_server as knowledge_server

    monkeypatch.setattr(knowledge_server, "_service", KnowledgeService(FakeMemory()))
    hits = asyncio.run(knowledge_server._search("退货", 3, 0.5))
    assert hits and hits[0]["doc_id"] == "POL-X"
    assert asyncio.run(knowledge_server._status())["status"] == "ready"


def test_mcp_service_initializes_knowledge(monkeypatch):
    import mcp_tools.knowledge_server as knowledge_server

    calls = []

    class InitMemory:
        documents = [{"content": "x"}]
        index_build_failed = False

        async def load_knowledge_base(self, path):
            calls.append(path)
            return 1

        async def search(self, query, top_k=5, min_score=0.5):
            return []

    monkeypatch.setattr(knowledge_server, "LongTermMemory", lambda: InitMemory())
    monkeypatch.setattr(knowledge_server, "_service", None)
    service = asyncio.run(knowledge_server._get_service())
    assert isinstance(service, KnowledgeService) and calls
    asyncio.run(knowledge_server._get_service())
    assert len(calls) == 1


def test_pi_comparison_with_fake_runners():
    cases = [{
        "id": "case-1",
        "question": "退货期限？",
        "expected_source": "POL-X",
        "expected_keywords": ["七天"],
        "expected_action": "answer",
    }]
    rag = FakeRunner(answer="七天 [POL-X v1.0 §A]", sources=["POL-X v1.0 §A"], latency_ms=100.0)
    pi = FakeRunner(answer="七天 [POL-X v1.0 §A]", sources=["POL-X v1.0 §A"], latency_ms=120.0, cost_usd=0.01)
    results = evaluate_runners(cases, rag, pi)
    summary = summarize(results)
    assert summary["rag"]["valid_cases"] == 1
    assert summary["pi"]["valid_cases"] == 1
    assert summary["rag"]["citation_full_accuracy"] == 1.0
    assert summary["pi"]["citation_full_accuracy"] == 1.0
    assert summary["comparison"]["citation_full_delta"] == 0.0
    # No clear win -> keep LangGraph.
    assert summary["decision"]["expand_pi"] is False
    assert summary["decision"]["decision_basis"] == "citation_full_string_match_only"

    better_pi = FakeRunner(answer="七天 [POL-X v1.0 §A]", sources=["POL-X v1.0 §A"], latency_ms=100.0, cost_usd=0.01)
    worse_rag = FakeRunner(answer="不知道", sources=[], latency_ms=150.0)
    better = summarize(evaluate_runners(cases, worse_rag, better_pi))
    assert better["comparison"]["citation_full_delta"] >= 0.05
    assert better["decision"]["expand_pi"] is True

    unknown_pi = FakeRunner(
        answer="七天 [POL-X v1.0 §A]",
        sources=["POL-X v1.0 §A"],
        latency_ms=100.0,
        cost_usd=None,
        cost_known=False,
    )
    unknown = summarize(evaluate_runners(cases, worse_rag, unknown_pi))
    assert unknown["pi"]["cost_known"] is False
    assert unknown["pi"]["cost_usd"] is None
    assert unknown["decision"]["expand_pi"] is False
    assert "成本未知" in unknown["decision"]["reason"]


class ScriptedRunner:
    def __init__(self, results):
        self.results = list(results)

    def run(self, question):
        return self.results.pop(0)


def test_comparison_excludes_failures_from_quality():
    cases = [
        {
            "id": "c1",
            "question": "q1",
            "expected_source": "POL-X",
            "expected_keywords": ["七天"],
            "expected_action": "answer",
        },
        {
            "id": "c2",
            "question": "q2",
            "expected_source": "POL-X",
            "expected_keywords": ["七天"],
            "expected_action": "answer",
        },
    ]
    correct = RunnerResult(
        answer="七天 [POL-X v1.0 §A]",
        sources=["POL-X v1.0 §A"],
        latency_ms=100.0,
        cost_usd=0.01,
        cost_known=True,
    )
    failure = RunnerResult(latency_ms=10.0, error="boom", cost_known=False)

    rag = ScriptedRunner([correct, failure])
    pi = ScriptedRunner([correct, correct])
    summary = summarize(evaluate_runners(cases, rag, pi))
    assert summary["rag"]["valid_cases"] == 1
    assert summary["rag"]["citation_full_accuracy"] == 1.0
    assert summary["rag"]["failure_rate"] == 0.5
    assert summary["pi"]["valid_cases"] == 2
    assert summary["pi"]["citation_full_accuracy"] == 1.0
    assert summary["decision"]["expand_pi"] is False

    # Pi failing a case must not be hidden by excluding it from quality.
    rag_ok = ScriptedRunner([correct, correct])
    pi_failing = ScriptedRunner([correct, failure])
    summary = summarize(evaluate_runners(cases, rag_ok, pi_failing))
    assert summary["pi"]["valid_cases"] == 1
    assert summary["pi"]["failure_rate"] == 0.5
    assert summary["decision"]["expand_pi"] is False
    assert "失败率" in summary["decision"]["reason"]


def test_subprocess_pi_runner_parses_json():
    code = (
        "import json;"
        "print(json.dumps({'answer':'a','sources':['s'],'latency_ms':12,"
        "'cost_usd':0.01,'cost_known':True,'input_tokens':1,'output_tokens':2,'error':''}))"
    )
    runner = SubprocessPiRunner([sys.executable, "-c", code])
    result = runner.run("q")
    assert result.answer == "a"
    assert result.sources == ["s"]
    assert result.cost_usd == 0.01
    assert result.cost_known is True
    assert result.input_tokens == 1
    assert result.error == ""


def test_pi_experiment_assets():
    package = json.loads((PI_DIR / "package.json").read_text(encoding="utf-8"))
    assert "@earendil-works/pi-agent-core" in package["dependencies"]
    assert "@earendil-works/pi-ai" in package["dependencies"]
    agent_source = (PI_DIR / "src" / "agent.ts").read_text(encoding="utf-8")
    assert "new Agent" in agent_source
    assert "knowledge_search" in agent_source
    assert "参考来源" in agent_source
    assert "cost_known" in agent_source
    assert (PI_DIR / "README.md").exists()


def test_langgraph_business_routes_retained():
    from api.main import app

    paths = app.openapi()["paths"]
    assert "/api/refunds" in paths
    assert "/api/workbench/refunds/{refund_id}/decision" in paths
    assert "/api/admin/overview" in paths
    assert "/api/knowledge/search" in paths

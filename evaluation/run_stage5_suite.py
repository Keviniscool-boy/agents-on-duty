"""Deterministic Stage 5 regression suite (no external model/API required)."""

import asyncio
import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain_core.messages import AIMessage, HumanMessage

from agents.knowledge_rag import KnowledgeRAGAgent
from evaluation.metrics import score_case
from evaluation.suite import build_report, write_report
from memory.long_term import LongTermMemory

EVALUATION_DIR = Path(__file__).resolve().parent

EXPECTED_DOC_IDS = {
    "POL-AFTER-MVP-001",
    "POL-AFTER-SPECIAL-001",
    "POL-MEMBER-001",
    "POL-LOGISTICS-001",
    "POL-PRODUCT-001",
    "POL-ACCOUNT-001",
    "POL-SERVICE-001",
}


class KeywordishEmbeddings:
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


def _check(name: str, passed: bool, detail="") -> dict:
    return {"case": name, "passed": bool(passed), "detail": str(detail)[:200]}


def check_manifest() -> dict:
    manifest = json.loads((EVALUATION_DIR / "manifest.json").read_text(encoding="utf-8"))
    covered = set()
    for suite in manifest["suites"].values():
        covered.update(suite.get("scenarios", []))
    missing = sorted(set(manifest["required_scenarios"]) - covered)
    return _check("manifest_scenarios", not missing, f"missing={missing}")


def check_policy_metadata(tmp: str) -> dict:
    memory = LongTermMemory(index_path=str(Path(tmp) / "index"))
    memory.embeddings = KeywordishEmbeddings()
    count = asyncio.run(memory.load_knowledge_base(str(PROJECT_ROOT / "knowledge_base")))
    doc_ids = {document["metadata"].get("doc_id") for document in memory.documents}
    missing = sorted(EXPECTED_DOC_IDS - doc_ids)
    return _check(
        "policy_metadata",
        count > 0 and not missing,
        f"documents={len(memory.documents)} missing={missing}",
    )


def check_hybrid(tmp: str) -> dict:
    memory = LongTermMemory(index_path=str(Path(tmp) / "hybrid"))
    memory.embeddings = KeywordishEmbeddings()
    asyncio.run(memory.add_document("alpha beta policy", source="alpha", metadata={}))
    asyncio.run(memory.add_document("gamma beta policy", source="gamma", metadata={}))
    results = asyncio.run(memory.search("beta", top_k=5, min_score=0.55))
    sources = [result["source"] for result in results]
    return _check("hybrid_retrieval", "alpha" in sources and "gamma" in sources, sources)


def check_refusal_and_handoff() -> dict:
    agent = KnowledgeRAGAgent(FakeLLM(["query"]), EmptyMemory())
    result = asyncio.run(agent.process({
        "messages": [HumanMessage(content="退款政策是什么")],
        "sub_results": {},
    }))
    sub = result["sub_results"]
    passed = sub.get("knowledge_decision") == "refuse" and sub.get("handoff_required") is True
    return _check("refusal_and_handoff", passed, sub)


def check_metrics() -> dict:
    answer = score_case(
        {"expected_source": "POL-A-001", "expected_action": "answer"},
        ["POL-A-001 v1 §x"],
        "根据 POL-A-001 v1 §x",
        {},
    )
    refuse = score_case(
        {"expected_source": None, "expected_action": "refuse"},
        [],
        "当前知识库没有相关信息，暂时无法确认。",
        {},
    )
    handoff = score_case(
        {"expected_source": None, "expected_action": "handoff"},
        [],
        "请联系人工客服。",
        {"handoff_required": True},
    )
    passed = all(answer.values()) and refuse["refusal_correct"] and handoff["handoff_correct"]
    return _check("metrics_scoring", passed, {"answer": answer, "refuse": refuse, "handoff": handoff})


def run_checks() -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        return [
            check_manifest(),
            check_policy_metadata(tmp),
            check_hybrid(tmp),
            check_refusal_and_handoff(),
            check_metrics(),
        ]


def main() -> int:
    results = run_checks()
    report = build_report("stage5", results, sample_size=len(results))
    path = write_report(report)
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(f"failures: {len(report['failures'])}")
    print(f"report: {path}")
    return 0 if not report["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Stable knowledge service shared by the HTTP API and MCP adapter."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_KB_DIR = Path(__file__).resolve().parents[1] / "knowledge_base"


class KnowledgeUnavailableError(RuntimeError):
    """Raised when the knowledge index cannot be built or has no documents."""


async def initialize_knowledge_memory(
    memory,
    kb_dir: str | Path | None = None,
    require_documents: bool = False,
) -> int:
    """Rebuild/refresh the persisted index before serving or comparing.

    The HTTP startup path loads the knowledge base explicitly; MCP and the
    evaluation baseline must do the same or they can silently use an empty or
    stale index.
    """
    count = await memory.load_knowledge_base(str(kb_dir or DEFAULT_KB_DIR))
    if getattr(memory, "index_build_failed", False):
        raise KnowledgeUnavailableError("knowledge index build failed")
    if require_documents and not getattr(memory, "documents", None):
        raise KnowledgeUnavailableError("knowledge base is empty")
    return count


@dataclass(frozen=True)
class KnowledgeHit:
    doc_id: str
    source: str
    content: str
    score: float
    retrieval: str = ""
    version: str = ""
    section: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class KnowledgeService:
    """Thin, content-only facade over the FAISS/keyword knowledge retriever.

    The same object backs both ``POST /api/knowledge/search`` and the MCP
    ``knowledge_search`` tool, so Pi and the LangGraph RAG agent read from one
    source of truth.
    """

    def __init__(self, memory):
        self.memory = memory

    async def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.5,
    ) -> list[KnowledgeHit]:
        if not query or not query.strip():
            return []
        safe_top_k = max(1, min(int(top_k), 20))
        safe_min_score = max(0.0, min(float(min_score), 1.0))
        documents = await self.memory.search(
            query,
            top_k=safe_top_k,
            min_score=safe_min_score,
        )
        return [self._to_hit(document) for document in documents]

    def status(self) -> dict:
        if hasattr(self.memory, "knowledge_status"):
            return {
                "status": self.memory.knowledge_status(),
                "documents": len(getattr(self.memory, "documents", []) or []),
            }
        return {"status": "unknown", "documents": 0}

    @staticmethod
    def _to_hit(document: dict[str, Any]) -> KnowledgeHit:
        metadata = document.get("metadata") or {}
        return KnowledgeHit(
            doc_id=str(metadata.get("doc_id") or ""),
            source=str(document.get("source") or ""),
            content=str(document.get("content") or ""),
            score=round(float(document.get("score") or 0.0), 4),
            retrieval=str(document.get("retrieval") or ""),
            version=str(metadata.get("version") or ""),
            section=str(metadata.get("section") or ""),
        )

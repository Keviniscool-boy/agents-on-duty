"""MCP adapter exposing the Python knowledge service to Pi/other agents."""

import asyncio
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowledge.service import (
    KnowledgeService,
    KnowledgeUnavailableError,
    initialize_knowledge_memory,
)
from memory.long_term import EmbeddingUnavailable, LongTermMemory

server = MCPServer(
    name="smart-cs-knowledge-server",
    version="1.0.0",
)

_service: KnowledgeService | None = None


async def _get_service() -> KnowledgeService:
    """Lazily build the index once before serving MCP requests."""
    global _service
    if _service is None:
        memory = LongTermMemory()
        await initialize_knowledge_memory(memory)
        _service = KnowledgeService(memory)
    return _service


async def _search(query: str, top_k: int = 5, min_score: float = 0.5) -> list[dict]:
    try:
        service = await _get_service()
        hits = await service.search(query, top_k, min_score)
    except (EmbeddingUnavailable, KnowledgeUnavailableError) as exc:
        return [{"error": "knowledge_unavailable", "message": str(exc)}]
    return [hit.to_dict() for hit in hits]


async def _status() -> dict:
    service = await _get_service()
    return service.status()


@server.tool(
    name="knowledge_search",
    description="检索已发布的企业知识库政策，返回来源、版本、章节与原文片段；证据不足时返回空列表",
)
async def knowledge_search(
    query: str,
    top_k: int = 5,
    min_score: float = 0.5,
) -> list[dict]:
    return await _search(query, top_k, min_score)


@server.tool(
    name="knowledge_status",
    description="查看知识库当前状态（ready/empty/degraded）与文档数量",
)
async def knowledge_status() -> dict:
    return await _status()


if __name__ == "__main__":
    asyncio.run(server.run_stdio_async())

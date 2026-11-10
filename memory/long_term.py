import json
import hashlib
import logging
import os
import re
from datetime import date
from pathlib import Path

import faiss
import numpy as np
import yaml
from dotenv import load_dotenv
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_openai import OpenAIEmbeddings


load_dotenv()
logger = logging.getLogger(__name__)


class EmbeddingUnavailable(Exception):
    """向量服务无法处理本次请求时抛出。"""


class _LazyEmbeddings:
    """延迟构造真实 embedding 客户端。

    api.main 在模块级实例化 LongTermMemory，构造即校验会让没配 key 的
    环境 import 就炸；这里把失败推迟到首次调用，与旧 DashScope 行为一致。
    """

    def __init__(self, factory):
        self._factory = factory
        self._inner = None

    async def aembed_query(self, text: str) -> list[float]:
        if self._inner is None:
            self._inner = self._factory()
        return await self._inner.aembed_query(text)


def _build_embeddings() -> tuple[object, dict]:
    """选择 embedding 实现。

    显式配置 DASHSCOPE_API_KEY 时优先走 DashScope 原生接口（保持向后兼容）；
    否则走 OpenAI 兼容接口（OPENAI_BASE_URL + OPENAI_API_KEY + EMBEDDING_MODEL），
    任何提供 /embeddings 的服务都可用。
    返回 (embeddings 实例, 供索引校验的身份标识)。
    """
    dashscope_key = os.getenv("DASHSCOPE_API_KEY")
    if dashscope_key:
        return (
            DashScopeEmbeddings(
                model=os.getenv("EMBEDDING_MODEL", "text-embedding-v3"),
                dashscope_api_key=dashscope_key,
            ),
            {
                "provider": "dashscope",
                "model": os.getenv("EMBEDDING_MODEL", "text-embedding-v3"),
            },
        )

    identity = {
        "provider": "openai-compatible",
        "model": os.getenv("EMBEDDING_MODEL", "text-embedding-v3"),
    }
    return (
        _LazyEmbeddings(
            lambda: OpenAIEmbeddings(
                model=os.getenv("EMBEDDING_MODEL", "text-embedding-v3"),
                # 空字符串视为未配置，交给 openai 客户端报出清晰错误。
                api_key=os.getenv("OPENAI_API_KEY") or None,
                base_url=os.getenv("OPENAI_BASE_URL") or None,
            )
        ),
        identity,
    )


class LongTermMemory:
    """基于真实 Embedding 和 FAISS 的长期知识库。"""

    def __init__(
        self,
        index_path: str = "./vector_store/faiss_index",
    ):
        self.index_path = Path(index_path)
        self.embeddings, self.embedding_identity = _build_embeddings()
        self.documents = []
        self.index = None
        self.embedding_available = None
        self.index_build_failed = False
        self._init_index()

    def _init_index(self) -> None:
        """从磁盘加载已有的 FAISS 索引和文档元数据。

        元数据里记录了建索引时的 embedding 身份（provider + model）；
        与当前配置不一致时丢弃索引——不同模型向量空间不同，混用会
        导致检索全错甚至维度崩溃，必须整库重建。
        """
        metadata_path = self.index_path.with_suffix(".meta.json")

        if not self.index_path.exists():
            return

        try:
            self.index = faiss.read_index(str(self.index_path))

            if metadata_path.exists():
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if isinstance(metadata, dict):
                    stored_identity = metadata.get("embedding", {})
                    if stored_identity != self.embedding_identity:
                        print(
                            "Embedding 配置已变更 "
                            f"({stored_identity} -> {self.embedding_identity})，"
                            "丢弃旧索引，将在启动时用当前模型重建。"
                        )
                        self.index = None
                        self.documents = []
                        return
                    self.documents = metadata.get("documents", [])
                else:
                    # 旧格式（纯文档列表）：无身份信息，默认可用。
                    self.documents = metadata
        except Exception:
            # 索引损坏时从空知识库开始，避免服务无法启动。
            self.index = None
            self.documents = []

    async def embed_text(self, text: str) -> list[float]:
        """调用配置的 Embedding 服务，将文本转换成向量。"""
        try:
            vector = await self.embeddings.aembed_query(text)
        except Exception as exc:
            self.embedding_available = False
            logger.warning("Embedding request failed (%s)", type(exc).__name__)
            raise EmbeddingUnavailable("Embedding service unavailable") from exc
        self.embedding_available = True
        return vector

    def knowledge_status(self) -> str:
        """只返回最近一次观测到的状态，不主动探测服务可用性。"""
        if self.index_build_failed or self.embedding_available is False:
            return "degraded"
        if not self.documents:
            return "empty"
        if self.embedding_available is None:
            return "unknown"
        return "ready"

    async def add_document(
        self,
        content: str,
        source: str = "",
        metadata: dict | None = None,
    ) -> str:
        """将一个文本块向量化并加入 FAISS，随后立即落盘（B3）。

        运行时通过本方法新增的知识必须持久化，否则重启即丢。
        """
        doc_id = await self._add_document(content, source, metadata)
        self.save()
        return doc_id

    async def _add_document(
        self,
        content: str,
        source: str = "",
        metadata: dict | None = None,
    ) -> str:
        """向量化并加入索引/内存，不落盘（批量场景由调用方统一 save）。"""
        doc_id = hashlib.md5(content.encode("utf-8")).hexdigest()[:12]

        vector = await self.embed_text(content)

        if self.index is None:
            self.index = faiss.IndexFlatIP(len(vector))

        vector_array = np.array([vector], dtype=np.float32)
        faiss.normalize_L2(vector_array)
        self.index.add(vector_array)

        self.documents.append(
            {
                "id": doc_id,
                "content": content,
                "source": source,
                "metadata": metadata or {},
            }
        )

        return doc_id

    # 批量添加多份文档
    async def add_documents_batch(
        self,
        documents: list[dict],
    ) -> list[str]:
        """批量添加多份文档"""
        # 保存每一份文档生成的ID
        doc_ids = []

        # 逐个处理文档
        for document in documents:
            doc_id = await self._add_document(
                content=document.get("content", ""),
                source=document.get("source", ""),
                metadata=document.get("metadata", {}),
            )
            doc_ids.append(doc_id)

        self.save()
        return doc_ids

    def save(self) -> None:
        """将 FAISS 索引和文档元数据持久化到磁盘。"""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

        if self.index is not None:
            faiss.write_index(self.index, str(self.index_path))
        elif self.index_path.exists():
            # 知识库被清空时删除旧索引文件，避免磁盘残留与元数据不一致。
            self.index_path.unlink()

        metadata_path = self.index_path.with_suffix(".meta.json")
        metadata_path.write_text(
            json.dumps(
                {
                    "embedding": self.embedding_identity,
                    "documents": self.documents,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    async def load_knowledge_base(self, kb_dir: str) -> int:
        """让索引与知识库目录中当前有效的文档及其元数据保持同步。"""
        kb_path = Path(kb_dir)
        documents = []
        published_ids = set()
        previous_index = (
            faiss.clone_index(self.index) if self.index is not None else None
        )
        previous_documents = list(self.documents)
        try:
            for file_path in sorted(kb_path.rglob("*")) if kb_path.exists() else []:
                if not file_path.is_file() or file_path.suffix.lower() not in {
                    ".txt",
                    ".md",
                }:
                    continue
                content = file_path.read_text(encoding="utf-8")
                if file_path.suffix.lower() == ".md":
                    parts = content.split("---", 2)
                    if len(parts) != 3 or parts[0].strip():
                        raise ValueError(f"Invalid front matter: {file_path.name}")
                    metadata = yaml.safe_load(parts[1])
                    if not isinstance(metadata, dict) or not all(
                        metadata.get(k) for k in ("doc_id", "version", "status")
                    ):
                        raise ValueError(f"Missing policy metadata: {file_path.name}")
                    if metadata["status"] != "published" or metadata.get(
                        "superseded_by"
                    ):
                        continue
                    start = date.fromisoformat(str(metadata["effective_date"]))
                    end = (
                        date.fromisoformat(str(metadata["expiration_date"]))
                        if metadata.get("expiration_date")
                        else None
                    )
                    if start > date.today() or (end and end < date.today()):
                        continue
                    if metadata["doc_id"] in published_ids:
                        raise ValueError(
                            f"Multiple active versions: {metadata['doc_id']}"
                        )
                    published_ids.add(metadata["doc_id"])
                    metadata = {
                        **metadata,
                        "effective_date": start.isoformat(),
                        "expiration_date": end.isoformat() if end else None,
                        "file": str(file_path.relative_to(kb_path)),
                    }
                    sections = re.split(r"(?m)^##\s+(.+?)\s*$", parts[2])
                    if len(sections) == 1:
                        # 没有二级标题时按整篇切分，避免政策被静默跳过。
                        sections = ["", "正文", sections[0]]
                    for i in range(1, len(sections), 2):
                        heading, body = sections[i], sections[i + 1].strip()
                        for chunk in self._chunk_text(body):
                            source = f"{metadata['doc_id']} v{metadata['version']} §{heading}"
                            documents.append(
                                {
                                    "content": chunk,
                                    "source": source,
                                    "metadata": {**metadata, "section": heading},
                                }
                            )
                else:
                    for chunk in self._chunk_text(content):
                        documents.append(
                            {
                                "content": chunk,
                                "source": file_path.name,
                                "metadata": {
                                    "file": str(file_path.relative_to(kb_path))
                                },
                            }
                        )
            existing = [
                {k: doc[k] for k in ("content", "source", "metadata")}
                for doc in self.documents
            ]
            if documents == existing and (not documents or self.index is not None):
                self.index_build_failed = False
                return 0
            self.index = None
            self.documents = []
            for document in documents:
                await self._add_document(**document)
        except (
            EmbeddingUnavailable,
            ValueError,
            TypeError,
            KeyError,
            OSError,
            yaml.YAMLError,
        ) as exc:
            self.index = previous_index
            self.documents = previous_documents
            self.index_build_failed = True
            logger.warning("Knowledge index update failed (%s)", type(exc).__name__)
            return 0
        self.index_build_failed = False
        self.save()
        return len(documents)

    @staticmethod
    def _chunk_text(
        text: str,
        chunk_size: int = 512,
        overlap: int = 128,
    ) -> list[str]:
        """按段落切分文本，并保留相邻文本块的重叠内容。"""
        paragraphs = text.split("\n\n")
        chunks = []
        current_chunk = ""

        for paragraph in paragraphs:
            paragraph = paragraph.strip()
            if not paragraph:
                continue

            candidate = f"{current_chunk}\n\n{paragraph}".strip()

            if len(candidate) <= chunk_size:
                current_chunk = candidate
                continue

            if current_chunk:
                chunks.append(current_chunk)

            overlap_text = current_chunk[-overlap:]
            current_chunk = f"{overlap_text}\n\n{paragraph}".strip()

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    @staticmethod
    def _terms(text: str) -> list[str]:
        """英文/数字按词切分，中文按字符二元组切分，供关键词检索使用。"""
        lowered = text.lower()
        terms: set[str] = set(re.findall(r"[a-z0-9%]+", lowered))
        for run in re.findall(r"[\u4e00-\u9fff]+", lowered):
            if len(run) == 1:
                terms.add(run)
            else:
                terms.update(run[i : i + 2] for i in range(len(run) - 1))
        return list(terms)

    def _keyword_hits(
        self,
        query: str,
        top_k: int,
        min_keyword_score: float = 0.3,
    ) -> list[tuple[dict, float]]:
        """关键词近邻检索：按查询词在文档中的命中比例打分。"""
        terms = self._terms(query)
        if not terms:
            return []
        today = date.today().isoformat()
        scored: list[tuple[dict, float]] = []
        for document in self.documents:
            metadata = document.get("metadata", {})
            if metadata.get("expiration_date") and metadata["expiration_date"] < today:
                continue
            content = document.get("content", "").lower()
            hits = sum(1 for term in terms if term in content)
            if not hits:
                continue
            score = hits / len(terms)
            if score >= min_keyword_score:
                scored.append((document, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]

    def _fuse(
        self,
        vector_hits: list[tuple[dict, float]],
        keyword_hits: list[tuple[dict, float]],
        top_k: int,
    ) -> list[dict]:
        """倒数排名融合：合并向量与关键词两路排名。"""
        fused: dict[str, dict] = {}
        for source, hits in (("vector", vector_hits), ("keyword", keyword_hits)):
            for rank, (document, score) in enumerate(hits):
                entry = fused.setdefault(
                    document["content"],
                    {
                        "document": document,
                        "vector_score": 0.0,
                        "keyword_score": 0.0,
                        "rrf": 0.0,
                    },
                )
                entry[f"{source}_score"] = max(entry[f"{source}_score"], score)
                entry["rrf"] += 1.0 / (60 + rank + 1)
        ordered = sorted(fused.values(), key=lambda entry: entry["rrf"], reverse=True)
        results = []
        for entry in ordered[:top_k]:
            document = entry["document"].copy()
            document["score"] = round(
                max(entry["vector_score"], entry["keyword_score"]), 4
            )
            document["vector_score"] = round(entry["vector_score"], 4)
            document["keyword_score"] = round(entry["keyword_score"], 4)
            document["retrieval"] = "hybrid"
            results.append(document)
        return results

    async def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.6,
        hybrid: bool = True,
    ) -> list[dict]:
        """向量 + 关键词混合检索；FAISS 不可用时退化为关键词检索。"""
        if self.index_build_failed:
            raise EmbeddingUnavailable("Knowledge index update did not complete")
        if not self.documents:
            return []
        if self.index is None:
            # B14：降级检索与 FAISS 路径同一阈值与分数语义（0~1）。
            return self._fallback_search(query, top_k, min_score)

        # 将用户问题转换为和文档相同空间的向量。
        query_vector = await self.embed_text(query)
        query_array = np.array([query_vector], dtype=np.float32)

        # 添加文档时做过归一化，查询向量也必须使用相同处理。
        faiss.normalize_L2(query_array)

        # 候选集放大后再融合，避免只取 top_k 漏掉关键词命中的文档。
        candidate_k = min(len(self.documents), max(top_k * 4, 20))
        scores, indices = self.index.search(query_array, candidate_k)

        vector_hits: list[tuple[dict, float]] = []
        for score, index in zip(scores[0], indices[0]):
            if index < 0 or index >= len(self.documents):
                continue
            if score < min_score:
                continue
            document = self.documents[index]
            metadata = document.get("metadata", {})
            if (
                metadata.get("expiration_date")
                and metadata["expiration_date"] < date.today().isoformat()
            ):
                continue
            vector_hits.append((document, float(score)))

        if not hybrid:
            return [
                {**document, "score": score, "retrieval": "vector"}
                for document, score in vector_hits[:top_k]
            ]

        keyword_hits = self._keyword_hits(query, candidate_k)
        return self._fuse(vector_hits, keyword_hits, top_k)

    def _fallback_search(
        self,
        query: str,
        top_k: int,
        min_score: float = 0.6,
    ) -> list[dict]:
        """当 FAISS 索引不可用时，使用简单的文本匹配进行搜索。

        B14：分数归一化为"命中词占比"（0~1），与 FAISS 路径的余弦
        相似度语义对齐，并同样应用 min_score 过滤——降级路径不得
        因为换实现而放宽召回标准。
        """
        scored = []
        query_terms = set(query.lower().split())
        if not query_terms:
            return []
        for document in self.documents:
            metadata = document.get("metadata", {})
            if (
                metadata.get("expiration_date")
                and metadata["expiration_date"] < date.today().isoformat()
            ):
                continue
            content = document["content"].lower()
            hits = sum(1 for term in query_terms if term in content)
            score = hits / len(query_terms)
            if score >= min_score:
                scored.append((score, document))

        scored.sort(
            key=lambda item: item[0],
            reverse=True,
        )

        results = []
        for score, document in scored[:top_k]:
            result = document.copy()
            result["score"] = float(score)
            results.append(result)

        return results

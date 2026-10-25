import json
import hashlib
import os
from pathlib import Path

import faiss
import numpy as np
from dotenv import load_dotenv
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_openai import OpenAIEmbeddings


load_dotenv()


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
                metadata = json.loads(
                    metadata_path.read_text(encoding="utf-8")
                )
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
        return await self.embeddings.aembed_query(text)

    async def add_document(
        self,
        content: str,
        source: str = "",
        metadata: dict | None = None,
    ) -> str:
        """将一个文本块向量化并加入 FAISS。"""
        doc_id = hashlib.md5(
            content.encode("utf-8")
        ).hexdigest()[:12]

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

    #批量添加多份文档
    async def add_documents_batch(
            self,
            documents:list[dict],
    ) -> list[str]:
        '''批量添加多份文档'''
        #保存每一份文档生成的ID
        doc_ids=[]

        #逐个处理文档
        for document in documents:
            doc_id=await self.add_document(
                content=document.get("content",""),
                source=document.get("source",""),
                metadata=document.get("metadata",{}),
            )
            doc_ids.append(doc_id)

        return doc_ids

    def save(self) -> None:
        """将 FAISS 索引和文档元数据持久化到磁盘。"""
        self.index_path.parent.mkdir(parents=True, exist_ok=True)

        if self.index is not None:
            faiss.write_index(self.index, str(self.index_path))

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
        """读取目录中的 txt 文件，切分后加入知识库。"""
        kb_path = Path(kb_dir)

        if not kb_path.exists():
            return 0

        count = 0
        existing_contents = {
            document.get("content", "")
            for document in self.documents
        }

        for file_path in kb_path.glob("**/*.txt"):
            content = file_path.read_text(encoding="utf-8")
            chunks = self._chunk_text(content)

            for chunk in chunks:
                if chunk in existing_contents:
                    continue

                await self.add_document(
                    content=chunk,
                    source=file_path.name,
                    metadata={"file": str(file_path)},
                )
                existing_contents.add(chunk)
                count += 1

        return count

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

    async def search(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.6,
    ) -> list[dict]:
        """根据问题搜索最相关的知识文档。"""
        if not self.documents:
            return []
        if self.index is None:
            return self._fallback_search(query,top_k)

        # 将用户问题转换为和文档相同空间的向量。
        query_vector = await self.embed_text(query)
        query_array = np.array([query_vector], dtype=np.float32)

        # 添加文档时做过归一化，查询向量也必须使用相同处理。
        faiss.normalize_L2(query_array)

        # FAISS 返回相似度分数和文档在索引中的下标。
        scores, indices = self.index.search(
            query_array,
            min(top_k, len(self.documents)),
        )

        results = []
        for score, index in zip(scores[0], indices[0]):
            if index < 0:
                continue

            if score < min_score:
                continue

            document = self.documents[index].copy()
            document["score"] = float(score)
            results.append(document)

        return results
    def _fallback_search(self, query: str, top_k: int) -> list[dict]:
        """当 FAISS 索引不可用时，使用简单的文本匹配进行搜索。"""
        scored = []
        query_terms = set(query.lower().split())
        for document in self.documents:
            content = document["content"].lower()
            score = sum(
                1
                for term in query_terms
                if term in content
            )
            if score > 0:
                scored.append((score,document))

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

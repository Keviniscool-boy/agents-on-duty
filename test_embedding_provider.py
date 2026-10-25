"""Embedding 提供商选择与索引身份校验的测试（不发起任何真实 API 调用）。"""

import json
import sys
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import faiss

import memory.long_term as long_term_module
from memory.long_term import LongTermMemory


def test_provider_selection_prefers_dashscope_when_key_set(monkeypatch):
    """配置了 DASHSCOPE_API_KEY 时走 DashScope 原生实现。"""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "ds-key")
    embeddings, identity = long_term_module._build_embeddings()
    assert identity["provider"] == "dashscope"
    assert identity["model"] == "text-embedding-v3"
    assert type(embeddings).__name__ == "DashScopeEmbeddings"


def test_provider_selection_falls_back_to_openai_compatible(monkeypatch):
    """未配置 DashScope 时走 OpenAI 兼容接口，复用 OPENAI_BASE_URL。"""
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.com/v1")
    embeddings, identity = long_term_module._build_embeddings()
    assert identity["provider"] == "openai-compatible"
    # 惰性包装：真实客户端在首次调用时才构造。
    inner = embeddings._factory()
    assert type(inner).__name__ == "OpenAIEmbeddings"


def _make_index_dir(tmp: Path, identity: dict | None, legacy: bool = False):
    """构造一个含索引和元数据的假知识库目录。"""
    index_path = tmp / f"faiss_index-{uuid.uuid4().hex[:8]}"
    index = faiss.IndexFlatIP(4)
    faiss.write_index(index, str(index_path))

    documents = [{"id": "d1", "content": "内容", "source": "a.txt", "metadata": {}}]
    if legacy:
        payload = documents  # 旧格式：纯列表
    else:
        payload = {"embedding": identity, "documents": documents}

    index_path.with_suffix(".meta.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return index_path


def test_identity_mismatch_discards_index(tmp_path, monkeypatch):
    """embedding 身份与索引记录不一致时丢弃索引（防止向量空间混用）。"""
    index_path = _make_index_dir(tmp_path, {"provider": "dashscope", "model": "old"})
    with mock.patch.object(
        long_term_module,
        "_build_embeddings",
        return_value=(mock.Mock(), {"provider": "openai-compatible", "model": "new"}),
    ):
        memory = LongTermMemory(index_path=str(index_path))
    assert memory.index is None
    assert memory.documents == []


def test_matching_identity_loads_index(tmp_path):
    """身份一致时正常加载。"""
    identity = {"provider": "openai-compatible", "model": "new"}
    index_path = _make_index_dir(tmp_path, identity)
    with mock.patch.object(
        long_term_module, "_build_embeddings", return_value=(mock.Mock(), identity)
    ):
        memory = LongTermMemory(index_path=str(index_path))
    assert memory.index is not None
    assert memory.documents[0]["id"] == "d1"


def test_legacy_metadata_format_still_loads(tmp_path):
    """旧格式（纯文档列表，无身份信息）向后兼容。"""
    index_path = _make_index_dir(tmp_path, None, legacy=True)
    with mock.patch.object(
        long_term_module,
        "_build_embeddings",
        return_value=(mock.Mock(), {"provider": "dashscope", "model": "m"}),
    ):
        memory = LongTermMemory(index_path=str(index_path))
    assert memory.index is not None
    assert memory.documents[0]["content"] == "内容"

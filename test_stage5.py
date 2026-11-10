"""阶段五测试：请求追踪、模型调用可观测、故障覆盖与评测套件。"""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from agents.compliance_checker import ComplianceCheckerAgent
from agents.ticket_handler import TicketHandlerAgent
from evaluation.suite import build_report, compare, corpus_info, dataset_info
from tracing.admin_audit import list_events, trace_scope
from tracing.llm import TracedChatModel
from tracing.otel_config import get_metrics


@pytest.fixture
def isolated_auth(tmp_path, monkeypatch):
    from auth import database

    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "auth.sqlite")
    database.init_database()
    return database


class ReplyLLM:
    def __init__(self, content: str):
        self.content = content

    async def ainvoke(self, messages, **kwargs):
        return AIMessage(content=self.content)


class InnerModel:
    model_name = "stage5-test-model"

    async def ainvoke(self, messages, **kwargs):
        return AIMessage(content="ok")


class BoomModel:
    model_name = "stage5-boom"

    async def ainvoke(self, messages, **kwargs):
        raise ValueError("boom")


def test_trace_id_header_is_generated_and_forwarded():
    from api.main import app

    client = TestClient(app)
    response = client.get("/health")
    trace_id = response.headers.get("x-trace-id")
    assert response.status_code == 200
    assert trace_id and len(trace_id) == 32

    forwarded = client.get("/health", headers={"X-Trace-Id": "custom-trace-id"})
    assert forwarded.headers.get("x-trace-id") == "custom-trace-id"


def test_traced_chat_model_records_metrics_and_event(isolated_auth):
    model = TracedChatModel(InnerModel(), "stage5-test-model")
    with trace_scope("stage5-trace", "actor", "session"):
        asyncio.run(model.ainvoke([]))

    summary = get_metrics().get_summary()
    assert summary["model_calls"]["stage5-test-model"]["total_calls"] >= 1
    events = list_events(20, kind="model", trace_id="stage5-trace")
    assert events[0]["action"] == "stage5-test-model"
    assert events[0]["outcome"] == "success"

    boom = TracedChatModel(BoomModel(), "stage5-boom")
    with trace_scope("stage5-boom-trace", "actor", "session"):
        with pytest.raises(ValueError):
            asyncio.run(boom.ainvoke([]))
    error_events = list_events(20, kind="model", trace_id="stage5-boom-trace")
    assert error_events[0]["outcome"] == "error"
    assert "ValueError" in error_events[0]["detail"]


def test_compliance_invalid_json_is_rejected():
    agent = ComplianceCheckerAgent(ReplyLLM("not json"))
    result = asyncio.run(agent.llm_check("问题", "回答", []))
    assert result.passed is False
    assert result.risk_level == "medium"
    assert "invalid JSON" in result.violations[0]


def test_mcp_timeout_falls_back_to_local_tool(monkeypatch, tmp_path):
    from mcp_tools.order_server import server as local_server

    monkeypatch.setenv("AFTER_SALES_DB_PATH", str(tmp_path / "after.sqlite"))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    class TimeoutMCP:
        async def list_tools(self):
            from types import SimpleNamespace

            return SimpleNamespace(tools=await local_server.list_tools())

        async def call_tool(self, name, arguments):
            raise TimeoutError("mcp timeout")

    class ToolLLM:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage(
                content="",
                tool_calls=[{"name": "order_list", "args": {}, "id": "call-1"}],
            )

    agent = TicketHandlerAgent(ToolLLM(), TimeoutMCP())
    result = asyncio.run(
        agent.process(
            {
                "messages": [HumanMessage(content="我的订单")],
                "user_id": "timeout-user",
                "session_id": "timeout-session",
                "sub_results": {},
            }
        )
    )
    tool_result = result["sub_results"]["ticket_handler_tool_results"][0]
    assert tool_result["fallback_used"] is True
    assert tool_result["error_type"] == "timeout"
    assert tool_result["tool"] == "order_list"


def test_suite_report_and_compare():
    dataset = {"sha256": "dataset", "cases": 2}
    corpus = {"sha256": "corpus", "documents": 7}
    report = build_report(
        "stage5",
        [{"case": "a", "passed": True}, {"case": "b", "passed": False}],
        dataset=dataset,
        corpus=corpus,
    )
    assert report["summary"] == {"passed": 1, "failed": 1}
    assert report["failures"][0]["case"] == "b"
    assert report["suite_version"]

    same = compare(
        report,
        build_report(
            "stage5",
            [{"case": "a", "passed": True}, {"case": "b", "passed": False}],
            dataset=dataset,
            corpus=corpus,
        ),
    )
    assert same["comparable"] is True

    different = build_report(
        "stage5",
        [{"case": "a", "passed": True}, {"case": "b", "passed": False}],
        dataset=dataset,
        corpus=corpus,
    )
    different["model"] = "another-model"
    mismatch = compare(report, different)
    assert mismatch["comparable"] is False
    assert "model" in mismatch["reasons"]


def test_compare_requires_same_dataset_corpus_and_size():
    dataset_a = {"sha256": "a", "cases": 1}
    dataset_b = {"sha256": "b", "cases": 1}
    corpus_a = {"sha256": "c", "documents": 7}
    corpus_b = {"sha256": "d", "documents": 7}
    base = build_report(
        "stage5", [{"passed": True}], dataset=dataset_a, corpus=corpus_a
    )
    same = build_report(
        "stage5", [{"passed": True}], dataset=dataset_a, corpus=corpus_a
    )
    assert compare(base, same)["comparable"] is True

    diff_dataset = build_report(
        "stage5", [{"passed": True}], dataset=dataset_b, corpus=corpus_a
    )
    result = compare(base, diff_dataset)
    assert result["comparable"] is False and "dataset" in result["reasons"]

    diff_corpus = build_report(
        "stage5", [{"passed": True}], dataset=dataset_a, corpus=corpus_b
    )
    assert "corpus" in compare(base, diff_corpus)["reasons"]

    diff_size = build_report(
        "stage5",
        [{"passed": True}, {"passed": False}],
        dataset={"sha256": "a", "cases": 2},
        corpus=corpus_a,
    )
    reasons = compare(base, diff_size)["reasons"]
    assert "sample_size" in reasons and "dataset_cases" in reasons


def test_compare_requires_complete_metadata():
    empty = build_report("stage5", [{"passed": True}])
    with_meta = build_report(
        "stage5",
        [{"passed": True}],
        dataset={"sha256": "a", "cases": 1},
        corpus={"sha256": "c", "documents": 7},
    )
    result = compare(empty, with_meta)
    assert result["comparable"] is False
    assert "dataset_missing" in result["reasons"]
    assert "corpus_missing" in result["reasons"]
    assert (
        compare(empty, build_report("stage5", [{"passed": True}]))["comparable"]
        is False
    )


def test_dataset_cases_must_match_sample_size():
    report = build_report(
        "stage5",
        [{"passed": True}],
        dataset={"sha256": "a", "cases": 2},
        corpus={"sha256": "c", "documents": 7},
    )
    result = compare(report, dict(report))
    assert result["comparable"] is False
    assert "dataset_cases_vs_sample_size" in result["reasons"]


def test_dataset_and_corpus_fingerprints(tmp_path):
    cases = tmp_path / "cases.jsonl"
    cases.write_text('{"id": "1"}\n{"id": "2"}\n', encoding="utf-8")
    info = dataset_info(cases)
    assert info["cases"] == 2 and len(info["sha256"]) == 64

    selected = dataset_info(cases, cases=[{"id": "1"}])
    assert selected["cases"] == 1 and selected["sha256"] != info["sha256"]

    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "a.md").write_text("alpha", encoding="utf-8")
    (kb / "ignore.json").write_text("{}", encoding="utf-8")
    corpus = corpus_info(kb)
    assert corpus["documents"] == 1 and len(corpus["sha256"]) == 64

    cases.write_text('{"id": "1"}\n', encoding="utf-8")
    assert dataset_info(cases)["sha256"] != info["sha256"]


def test_manifest_covers_required_scenarios():
    manifest = json.loads(
        (Path(__file__).parent / "evaluation" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    covered = set()
    for suite in manifest["suites"].values():
        covered.update(suite.get("scenarios", []))
    assert set(manifest["required_scenarios"]) <= covered


def test_stage5_suite_checks_pass():
    from evaluation.run_stage5_suite import run_checks

    results = run_checks()
    assert results and all(item["passed"] for item in results), results

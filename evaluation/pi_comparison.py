"""对比实验知识智能体与现有检索问答基线。

双方通过同一个知识服务读取同一套政策语料。对比判定刻意保守：
只有当实验方案在引用质量上明显胜出、且时延/成本/失败率都没有
退化时，才建议扩大使用范围。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evaluation.metrics import score_case
from evaluation.suite import corpus_info, dataset_info, environment, write_report
from knowledge.service import KnowledgeUnavailableError, initialize_knowledge_memory

EVALUATION_DIR = Path(__file__).resolve().parent
PI_DIR = PROJECT_ROOT / "experiments" / "pi_knowledge_agent"


@dataclass
class RunnerResult:
    answer: str = ""
    sources: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    cost_usd: float | None = None
    cost_known: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str = ""


class SubprocessPiRunner:
    """以子进程方式调用实验子项目的命令行，读取其结构化输出。"""

    def __init__(self, command: list[str] | None = None, env: dict | None = None):
        self.command = command or ["node", str(PI_DIR / "dist" / "index.js")]
        self.env = env or {}

    def run(self, question: str) -> RunnerResult:
        started = time.perf_counter()
        try:
            completed = subprocess.run(
                [*self.command, "--question", question, "--json"],
                capture_output=True,
                text=True,
                timeout=180,
                cwd=str(PROJECT_ROOT),
                env={**os.environ, **self.env},
            )
        except Exception as exc:  # noqa: BLE001 - reported as a failure result
            return RunnerResult(
                latency_ms=(time.perf_counter() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}",
            )

        latency_ms = (time.perf_counter() - started) * 1000
        lines = [line for line in completed.stdout.strip().splitlines() if line.strip()]
        payload: dict | None = None
        if lines:
            try:
                payload = json.loads(lines[-1])
            except json.JSONDecodeError:
                payload = None
        if payload is None:
            return RunnerResult(
                latency_ms=latency_ms,
                error=(completed.stderr or completed.stdout or "invalid JSON output").strip()[:500],
            )
        cost_known = bool(payload.get("cost_known", False))
        return RunnerResult(
            answer=str(payload.get("answer", "")),
            sources=list(payload.get("sources", []) or []),
            latency_ms=float(payload.get("latency_ms") or latency_ms),
            cost_usd=payload.get("cost_usd") if cost_known else None,
            cost_known=cost_known,
            input_tokens=payload.get("input_tokens"),
            output_tokens=payload.get("output_tokens"),
            error=str(payload.get("error", "") or ""),
        )


class LangGraphRagRunner:
    """进程内基线：复用现有的检索问答智能体。"""

    def __init__(self):
        from langchain_core.messages import HumanMessage
        from langchain_openai import ChatOpenAI

        from agents.knowledge_rag import KnowledgeRAGAgent
        from memory.long_term import LongTermMemory

        self._HumanMessage = HumanMessage
        self.memory = LongTermMemory()
        try:
            asyncio.run(
                initialize_knowledge_memory(self.memory, require_documents=True)
            )
        except KnowledgeUnavailableError as exc:
            raise RuntimeError(f"knowledge baseline unavailable: {exc}") from exc
        llm = ChatOpenAI(
            model=os.getenv("MODEL_NAME"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL"),
            temperature=0,
        )
        self.agent = KnowledgeRAGAgent(llm, self.memory)

    def run(self, question: str) -> RunnerResult:
        started = time.perf_counter()
        try:
            result = asyncio.run(
                self.agent.process({
                    "messages": [self._HumanMessage(content=question)],
                    "sub_results": {},
                })
            )
        except Exception as exc:  # noqa: BLE001 - reported as a failure result
            return RunnerResult(
                latency_ms=(time.perf_counter() - started) * 1000,
                error=f"{type(exc).__name__}: {exc}",
            )
        sub_results = result.get("sub_results", {})
        return RunnerResult(
            answer=str(result.get("final_response", "")),
            sources=list(sub_results.get("sources", []) or []),
            latency_ms=(time.perf_counter() - started) * 1000,
        )


def load_cases(limit: int | None = None) -> list[dict]:
    path = EVALUATION_DIR / "rag_cases.jsonl"
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return cases[:limit] if limit else cases


def _answer_correct(case: dict, answer: str) -> bool:
    keywords = case.get("expected_keywords") or []
    return all(keyword in answer for keyword in keywords)


def evaluate_runners(cases: list[dict], rag_runner, pi_runner) -> list[dict]:
    results = []
    for case in cases:
        rag = rag_runner.run(case["question"])
        pi = pi_runner.run(case["question"])
        rag_metrics = score_case(case, rag.sources, rag.answer, {})
        pi_metrics = score_case(case, pi.sources, pi.answer, {})
        results.append({
            "id": case["id"],
            "question": case["question"],
            "rag": {
                **rag.__dict__,
                **rag_metrics,
                "answer_correct": _answer_correct(case, rag.answer),
            },
            "pi": {
                **pi.__dict__,
                **pi_metrics,
                "answer_correct": _answer_correct(case, pi.answer),
            },
        })
    return results


def _side_summary(results: list[dict], side: str) -> dict:
    rows = [item[side] for item in results]
    total = len(rows) or 1
    # 失败请求单独统计并排除在质量分之外：
    # 否则空回答/空引用会悄悄拉低分母，掩盖真实质量差异。
    valid_rows = [row for row in rows if not row.get("error")]
    valid_total = len(valid_rows) or 1
    known_costs = [
        row["cost_usd"]
        for row in rows
        if row.get("cost_known") and row.get("cost_usd") is not None
    ]
    cost_known = bool(rows) and len(known_costs) == len(rows)
    return {
        "valid_cases": len(valid_rows),
        "answer_accuracy": round(sum(1 for row in valid_rows if row["answer_correct"]) / valid_total, 4),
        "citation_accuracy": round(sum(1 for row in valid_rows if row.get("citation_correct")) / valid_total, 4),
        "citation_full_accuracy": round(sum(1 for row in valid_rows if row.get("citation_full")) / valid_total, 4),
        "refusal_accuracy": round(sum(1 for row in valid_rows if row.get("refusal_correct")) / valid_total, 4),
        "median_latency_ms": round(median([row["latency_ms"] for row in valid_rows]) if valid_rows else 0.0, 2),
        "cost_usd": round(sum(known_costs), 6) if cost_known else None,
        "cost_known": cost_known,
        "cost_unknown_cases": len(rows) - len(known_costs),
        "failures": sum(1 for row in rows if row.get("error")),
        "failure_rate": round(sum(1 for row in rows if row.get("error")) / total, 4),
    }


DECISION_BASIS = "citation_full_string_match_only"


def decide(pi: dict, rag: dict, comparison: dict, max_cost_usd: float = 0.05) -> dict:
    """只有实验方案明显胜出且无退化时，才建议扩大其使用范围。"""
    if not pi.get("valid_cases") or not rag.get("valid_cases"):
        return {
            "expand_pi": False,
            "reason": "有效评测样本不足，不能扩大使用范围",
            "decision_basis": DECISION_BASIS,
        }
    better = (
        comparison.get("citation_full_delta", 0.0) >= 0.05
        and pi["failure_rate"] <= rag["failure_rate"]
        and pi["median_latency_ms"] <= max(rag["median_latency_ms"] * 1.5, 1.0)
    )
    cost_known = bool(pi.get("cost_known")) and pi.get("cost_usd") is not None
    affordable = cost_known and pi["cost_usd"] <= max_cost_usd
    if better and affordable:
        return {
            "expand_pi": True,
            "reason": "Pi 在完整来源字符串匹配上明显更好，且延迟/成本/失败率可接受",
            "decision_basis": DECISION_BASIS,
        }
    reasons = []
    if comparison.get("citation_full_delta", 0.0) < 0.05:
        reasons.append("完整来源字符串匹配提升不足 5 个百分点")
    if pi["failure_rate"] > rag["failure_rate"]:
        reasons.append("失败率高于基线")
    if pi["median_latency_ms"] > max(rag["median_latency_ms"] * 1.5, 1.0):
        reasons.append("中位延迟超过基线 1.5 倍")
    if not cost_known:
        reasons.append("Pi 成本未知，不能据此扩大使用范围")
    elif not affordable:
        reasons.append("单套件成本超预算")
    return {
        "expand_pi": False,
        "reason": "；".join(reasons) or "未达到扩展标准",
        "decision_basis": DECISION_BASIS,
    }


def summarize(results: list[dict]) -> dict:
    rag = _side_summary(results, "rag")
    pi = _side_summary(results, "pi")
    cost_delta = (
        round(pi["cost_usd"] - rag["cost_usd"], 6)
        if pi.get("cost_known") and rag.get("cost_known")
        and pi.get("cost_usd") is not None and rag.get("cost_usd") is not None
        else None
    )
    comparison = {
        "answer_delta": round(pi["answer_accuracy"] - rag["answer_accuracy"], 4),
        "citation_delta": round(pi["citation_accuracy"] - rag["citation_accuracy"], 4),
        "citation_full_delta": round(pi["citation_full_accuracy"] - rag["citation_full_accuracy"], 4),
        "latency_delta_ms": round(pi["median_latency_ms"] - rag["median_latency_ms"], 2),
        "cost_delta_usd": cost_delta,
        "failure_delta": round(pi["failure_rate"] - rag["failure_rate"], 4),
    }
    return {
        "rag": rag,
        "pi": pi,
        "comparison": comparison,
        "decision": decide(pi, rag, comparison),
    }


def run_comparison(cases: list[dict], rag_runner, pi_runner) -> dict:
    results = evaluate_runners(cases, rag_runner, pi_runner)
    summary = summarize(results)
    report = {
        **environment(),
        "suite": "pi_comparison",
        "sample_size": len(cases),
        "dataset": dataset_info(EVALUATION_DIR / "rag_cases.jsonl", cases=cases),
        "corpus": corpus_info(PROJECT_ROOT / "knowledge_base"),
        "summary": summary,
        "results": results,
        "failures": [
            {"id": item["id"], "side": side, "error": item[side]["error"]}
            for item in results
            for side in ("rag", "pi")
            if item[side].get("error")
        ],
    }
    return report


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Compare Pi vs LangGraph RAG")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--pi-command", default="")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    cases = load_cases(args.limit)
    pi_runner = SubprocessPiRunner(args.pi_command.split() if args.pi_command else None)
    report = run_comparison(cases, LangGraphRagRunner(), pi_runner)
    path = write_report(report, args.output or None)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

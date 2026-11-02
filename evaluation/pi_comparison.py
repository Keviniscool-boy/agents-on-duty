"""Compare the Pi knowledge agent with the LangGraph RAG baseline.

Both sides read the same policy corpus through the Python knowledge service.
The comparison is intentionally conservative: Pi is only recommended for wider
use when it clearly wins on citation quality without latency/cost/failure
regressions.
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
from evaluation.suite import environment, write_report

EVALUATION_DIR = Path(__file__).resolve().parent
PI_DIR = PROJECT_ROOT / "experiments" / "pi_knowledge_agent"


@dataclass
class RunnerResult:
    answer: str = ""
    sources: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str = ""


class SubprocessPiRunner:
    """Runs ``experiments/pi_knowledge_agent`` via its JSON CLI."""

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
        return RunnerResult(
            answer=str(payload.get("answer", "")),
            sources=list(payload.get("sources", []) or []),
            latency_ms=float(payload.get("latency_ms") or latency_ms),
            cost_usd=payload.get("cost_usd"),
            input_tokens=payload.get("input_tokens"),
            output_tokens=payload.get("output_tokens"),
            error=str(payload.get("error", "") or ""),
        )


class LangGraphRagRunner:
    """In-process baseline using the existing KnowledgeRAGAgent."""

    def __init__(self):
        from langchain_core.messages import HumanMessage
        from langchain_openai import ChatOpenAI

        from agents.knowledge_rag import KnowledgeRAGAgent
        from memory.long_term import LongTermMemory

        self._HumanMessage = HumanMessage
        self.memory = LongTermMemory()
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
    return {
        "answer_accuracy": round(sum(1 for row in rows if row["answer_correct"]) / total, 4),
        "citation_accuracy": round(sum(1 for row in rows if row["citation_correct"]) / total, 4),
        "refusal_accuracy": round(sum(1 for row in rows if row["refusal_correct"]) / total, 4),
        "median_latency_ms": round(median([row["latency_ms"] for row in rows]) if rows else 0.0, 2),
        "cost_usd": round(sum((row.get("cost_usd") or 0.0) for row in rows), 6),
        "failures": sum(1 for row in rows if row.get("error")),
        "failure_rate": round(sum(1 for row in rows if row.get("error")) / total, 4),
    }


def decide(pi: dict, rag: dict, comparison: dict, max_cost_usd: float = 0.05) -> dict:
    """Only recommend expanding Pi when it clearly wins without regressions."""
    better = (
        comparison["citation_delta"] >= 0.05
        and pi["failure_rate"] <= rag["failure_rate"]
        and pi["median_latency_ms"] <= max(rag["median_latency_ms"] * 1.5, 1.0)
    )
    affordable = pi["cost_usd"] <= max_cost_usd
    if better and affordable:
        return {"expand_pi": True, "reason": "Pi 在引用质量上明显更好，且延迟/成本/失败率可接受"}
    reasons = []
    if comparison["citation_delta"] < 0.05:
        reasons.append("引用准确率提升不足 5 个百分点")
    if pi["failure_rate"] > rag["failure_rate"]:
        reasons.append("失败率高于基线")
    if pi["median_latency_ms"] > max(rag["median_latency_ms"] * 1.5, 1.0):
        reasons.append("中位延迟超过基线 1.5 倍")
    if not affordable:
        reasons.append("单套件成本超预算")
    return {"expand_pi": False, "reason": "；".join(reasons) or "未达到扩展标准"}


def summarize(results: list[dict]) -> dict:
    rag = _side_summary(results, "rag")
    pi = _side_summary(results, "pi")
    comparison = {
        "answer_delta": round(pi["answer_accuracy"] - rag["answer_accuracy"], 4),
        "citation_delta": round(pi["citation_accuracy"] - rag["citation_accuracy"], 4),
        "latency_delta_ms": round(pi["median_latency_ms"] - rag["median_latency_ms"], 2),
        "cost_delta_usd": round(pi["cost_usd"] - rag["cost_usd"], 6),
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

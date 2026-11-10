import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from auth.database import create_user
from auth.security import create_access_token
from evaluation.metrics import score_case
from memory.long_term import LongTermMemory


load_dotenv(PROJECT_ROOT / ".env")
API_URL = os.getenv("EVALUATION_API_URL", "http://127.0.0.1:8000")
CASES_PATH = Path(__file__).with_name("rag_cases.jsonl")
RESULTS_DIR = Path(__file__).with_name("results")


def load_cases() -> list[dict]:
    return [
        json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines()
    ]


def token_for_eval_user() -> str:
    username = f"rag_eval_{uuid.uuid4().hex[:10]}"
    user = create_user(username, "rag123456")
    return create_access_token(user["id"], user["username"])


async def main() -> None:
    cases = load_cases()
    memory = LongTermMemory()
    results = []

    # 评测本机服务时不读取系统代理，避免 localhost 请求被代理返回 502。
    async with httpx.AsyncClient(
        base_url=API_URL,
        timeout=120,
        trust_env=False,
    ) as client:
        headers = {"Authorization": f"Bearer {token_for_eval_user()}"}

        for case in cases:
            started = time.perf_counter()
            # 与 KnowledgeRAGAgent 保持一致，评测实际线上使用的检索阈值。
            retrieved = await memory.search(case["question"], top_k=3, min_score=0.55)
            response = await client.post(
                "/api/chat",
                headers=headers,
                json={
                    "message": case["question"],
                    "session_id": f"rag-{case['id']}-{uuid.uuid4().hex[:8]}",
                },
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

            answer = response.json().get("response", "") if response.is_success else ""
            response_data = response.json() if response.is_success else {}
            sources = [item.get("source") for item in retrieved]
            metrics = score_case(case, sources, answer, response_data)
            answer_correct = all(
                keyword in answer for keyword in case.get("expected_keywords", [])
            )

            item = {
                **case,
                "retrieved_sources": sources,
                **metrics,
                "answer": answer,
                "answer_correct": answer_correct,
                "intent": response_data.get("intent", ""),
                "current_agent": response_data.get("current_agent", ""),
                "compliance_passed": response_data.get("compliance_passed"),
                "compliance_reason": response_data.get("compliance_reason", ""),
                "elapsed_ms": elapsed_ms,
                "status_code": response.status_code,
            }
            results.append(item)
            print(
                f"{case['id']}: recall={metrics['recall_at_k']} "
                f"citation={metrics['citation_correct']} "
                f"citation_full={metrics['citation_full']} "
                f"refusal={metrics['refusal_correct']} handoff={metrics['handoff_correct']} "
                f"answer={answer_correct} latency={elapsed_ms}ms"
            )

    total = len(results)

    def rate(key: str) -> float:
        return (
            round(sum(1 for item in results if item.get(key)) / total * 100, 2)
            if total
            else 0.0
        )

    summary = {
        "metric": "rag_evaluation_stage4",
        "total": total,
        "recall_at_k_percent": rate("recall_at_k"),
        "citation_correct_rate_percent": rate("citation_correct"),
        "citation_full_rate_percent": rate("citation_full"),
        "refusal_accuracy_percent": rate("refusal_correct"),
        "handoff_accuracy_percent": rate("handoff_correct"),
        "answer_accuracy_percent": rate("answer_correct"),
        "results": results,
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    output_path = RESULTS_DIR / "rag-results.json"
    output_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n召回率@k: {summary['recall_at_k_percent']}%")
    print(f"来源匹配率（初步）: {summary['citation_correct_rate_percent']}%")
    print(
        f"完整来源字符串匹配率（非断言级验证）: {summary['citation_full_rate_percent']}%"
    )
    print(f"拒答准确率: {summary['refusal_accuracy_percent']}%")
    print(f"转人工准确率: {summary['handoff_accuracy_percent']}%")
    print(f"回答关键词命中率: {summary['answer_accuracy_percent']}%")
    print(f"详细结果: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())

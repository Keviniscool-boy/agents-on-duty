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


load_dotenv(PROJECT_ROOT / ".env")
API_URL = os.getenv("EVALUATION_API_URL", "http://127.0.0.1:8000")
CASES_PATH = Path(__file__).with_name("compliance_cases.jsonl")
RESULTS_DIR = Path(__file__).with_name("results")


def load_cases() -> list[dict]:
    return [
        json.loads(line)
        for line in CASES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def token_for_eval_user() -> str:
    username = f"compliance_eval_{uuid.uuid4().hex[:10]}"
    user = create_user(username, "compliance123")
    return create_access_token(user["id"], user["username"])


async def main() -> None:
    cases = load_cases()
    results = []

    async with httpx.AsyncClient(base_url=API_URL, timeout=120) as client:
        headers = {"Authorization": f"Bearer {token_for_eval_user()}"}

        for case in cases:
            started = time.perf_counter()
            response = await client.post(
                "/api/chat",
                headers=headers,
                json={
                    "message": case["question"],
                    "session_id": f"compliance-{case['id']}-{uuid.uuid4().hex[:8]}",
                },
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            data = response.json() if response.is_success else {}

            passed = data.get("compliance_passed")
            correct = response.is_success and passed == case["expected_passed"]
            item = {
                **case,
                "actual_passed": passed,
                "correct": correct,
                "intent": data.get("intent", ""),
                "response": data.get("response", ""),
                "compliance_reason": data.get("compliance_reason", ""),
                "elapsed_ms": elapsed_ms,
                "status_code": response.status_code,
            }
            results.append(item)
            print(
                f"{case['id']}: expected_passed={case['expected_passed']} "
                f"actual_passed={passed} correct={correct} latency={elapsed_ms}ms"
            )

    correct_count = sum(item["correct"] for item in results)
    summary = {
        "metric": "compliance_evaluation",
        "total": len(results),
        "accuracy_percent": round(correct_count / len(results) * 100, 2),
        "results": results,
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    output_path = RESULTS_DIR / "compliance-results.json"
    output_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n合规判断准确率: {summary['accuracy_percent']}%")
    print(f"详细结果: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())

import json
import os
import time
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
API_URL = os.getenv("EVALUATION_API_URL", "http://127.0.0.1:8000")
CASES_PATH = Path(__file__).with_name("routing_cases.jsonl")
RESULTS_DIR = Path(__file__).with_name("results")


def load_cases() -> list[dict]:
    return [json.loads(line) for line in CASES_PATH.read_text(encoding="utf-8").splitlines()]


def register(client: httpx.Client) -> str:
    username = f"eval_{uuid.uuid4().hex[:10]}"
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "eval123456"},
    )
    response.raise_for_status()
    return response.json()["access_token"]


def main() -> None:
    cases = load_cases()
    results = []

    with httpx.Client(base_url=API_URL, timeout=120) as client:
        token = register(client)
        headers = {"Authorization": f"Bearer {token}"}

        for case in cases:
            started = time.perf_counter()
            response = client.post(
                "/api/chat",
                headers=headers,
                json={
                    "message": case["question"],
                    "session_id": f"eval-{case['id']}-{uuid.uuid4().hex[:8]}",
                },
            )
            elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

            if response.is_success:
                actual = response.json()
                actual_intent = actual.get("intent", "")
                result = {
                    **case,
                    "actual_intent": actual_intent,
                    "current_agent": actual.get("current_agent", ""),
                    "correct": actual_intent == case["expected_intent"],
                    "elapsed_ms": elapsed_ms,
                    "status_code": response.status_code,
                }
            else:
                result = {
                    **case,
                    "actual_intent": "",
                    "current_agent": "",
                    "correct": False,
                    "elapsed_ms": elapsed_ms,
                    "status_code": response.status_code,
                    "error": response.text,
                }

            results.append(result)
            print(
                f"{case['id']}: expected={case['expected_intent']} "
                f"actual={result['actual_intent']} correct={result['correct']} "
                f"latency={elapsed_ms}ms"
            )

    correct = sum(item["correct"] for item in results)
    accuracy = round(correct / len(results) * 100, 2) if results else 0
    summary = {
        "metric": "supervisor_intent_routing_accuracy",
        "total": len(results),
        "correct": correct,
        "accuracy_percent": accuracy,
        "results": results,
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    output_path = RESULTS_DIR / "routing-results.json"
    output_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n准确率: {correct}/{len(results)} = {accuracy}%")
    print(f"详细结果: {output_path}")


if __name__ == "__main__":
    main()

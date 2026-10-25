import asyncio
import json
import os
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")
API_URL = os.getenv("EVALUATION_API_URL", "http://127.0.0.1:8000")
RESULTS_DIR = Path(__file__).with_name("results")


async def main() -> None:
    results = []
    username_a = f"auth_eval_a_{uuid.uuid4().hex[:8]}"
    username_b = f"auth_eval_b_{uuid.uuid4().hex[:8]}"
    password = "auth123456"

    async with httpx.AsyncClient(base_url=API_URL, timeout=120) as client:
        register_a = await client.post(
            "/api/auth/register",
            json={"username": username_a, "password": password},
        )
        token_a = register_a.json()["access_token"]
        results.append({
            "case": "register",
            "passed": register_a.status_code == 201,
            "status_code": register_a.status_code,
        })

        duplicate = await client.post(
            "/api/auth/register",
            json={"username": username_a, "password": password},
        )
        results.append({
            "case": "duplicate_register_rejected",
            "passed": duplicate.status_code == 409,
            "status_code": duplicate.status_code,
        })

        login = await client.post(
            "/api/auth/login",
            json={"username": username_a, "password": password},
        )
        results.append({
            "case": "login",
            "passed": login.status_code == 200 and "access_token" in login.json(),
            "status_code": login.status_code,
        })

        wrong_login = await client.post(
            "/api/auth/login",
            json={"username": username_a, "password": "wrong-password"},
        )
        results.append({
            "case": "wrong_password_rejected",
            "passed": wrong_login.status_code == 401,
            "status_code": wrong_login.status_code,
        })

        no_token = await client.get("/api/auth/me")
        invalid_token = await client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer invalid-token"},
        )
        results.extend([
            {
                "case": "missing_token_rejected",
                "passed": no_token.status_code == 401,
                "status_code": no_token.status_code,
            },
            {
                "case": "invalid_token_rejected",
                "passed": invalid_token.status_code == 401,
                "status_code": invalid_token.status_code,
            },
        ])

        register_b = await client.post(
            "/api/auth/register",
            json={"username": username_b, "password": password},
        )
        token_b = register_b.json()["access_token"]

        chat = await client.post(
            "/api/chat",
            headers={"Authorization": f"Bearer {token_a}"},
            json={
                "message": "退款政策是什么？",
                "session_id": f"auth-eval-{uuid.uuid4().hex[:8]}",
            },
        )
        session_id = chat.json().get("session_id")
        own_history = await client.get(
            f"/api/history/{session_id}",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        other_history = await client.get(
            f"/api/history/{session_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        other_chat = await client.post(
            "/api/chat",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"message": "读取这个会话", "session_id": session_id},
        )
        results.extend([
            {
                "case": "owner_can_read_history",
                "passed": own_history.status_code == 200,
                "status_code": own_history.status_code,
            },
            {
                "case": "other_user_cannot_read_history",
                "passed": other_history.status_code == 403,
                "status_code": other_history.status_code,
            },
            {
                "case": "other_user_cannot_reuse_session",
                "passed": other_chat.status_code == 403,
                "status_code": other_chat.status_code,
            },
        ])

    passed = sum(item["passed"] for item in results)
    summary = {
        "metric": "auth_session_evaluation",
        "total": len(results),
        "passed": passed,
        "accuracy_percent": round(passed / len(results) * 100, 2),
        "results": results,
    }
    RESULTS_DIR.mkdir(exist_ok=True)
    output_path = RESULTS_DIR / "auth-results.json"
    output_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for item in results:
        print(f"{item['case']}: passed={item['passed']} status={item['status_code']}")
    print(f"\n鉴权与会话测试通过率: {summary['accuracy_percent']}%")
    print(f"详细结果: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())

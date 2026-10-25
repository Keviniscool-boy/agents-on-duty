import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agents.compliance_checker import ComplianceCheckerAgent


CASES = [
    {"id": "rule-001", "content": "退款政策是签收后7天内可以申请。", "expected_passed": True},
    {"id": "rule-002", "content": "请联系手机号13800138000。", "expected_passed": False},
    {"id": "rule-003", "content": "身份证号为110101199001011234。", "expected_passed": False},
    {"id": "rule-004", "content": "银行卡号为6222021234567890123。", "expected_passed": False},
    {"id": "rule-005", "content": "联系邮箱test@example.com。", "expected_passed": False},
    {"id": "rule-006", "content": "本产品 guaranteed profit，risk-free investment。", "expected_passed": False},
]


async def main() -> None:
    checker = ComplianceCheckerAgent(None)
    results = []

    for case in CASES:
        result = await checker.rule_check(case["content"])
        correct = result.passed == case["expected_passed"]
        item = {
            **case,
            "actual_passed": result.passed,
            "correct": correct,
            "violations": result.violations,
        }
        results.append(item)
        print(
            f"{case['id']}: expected_passed={case['expected_passed']} "
            f"actual_passed={result.passed} correct={correct}"
        )

    accuracy = round(
        sum(item["correct"] for item in results) / len(results) * 100,
        2,
    )
    output = {
        "metric": "compliance_rule_evaluation",
        "total": len(results),
        "accuracy_percent": accuracy,
        "results": results,
    }
    path = Path(__file__).with_name("results") / "compliance-rule-results.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n规则检查准确率: {accuracy}%")
    print(f"详细结果: {path}")


if __name__ == "__main__":
    asyncio.run(main())

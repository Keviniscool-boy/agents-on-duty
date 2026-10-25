import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from memory.long_term import LongTermMemory
from memory.short_term import ShortTermMemory
from memory.working_memory import WorkingMemory


async def test_short_term() -> list[dict]:
    results = []

    # 使用不可用端口，稳定验证 Redis 不可用时的内存降级。
    fallback = ShortTermMemory(
        redis_url="redis://127.0.0.1:6399/0",
        max_turns=2,
    )
    await fallback.add_message("session-a", "user", "第一条")
    await fallback.add_message("session-a", "assistant", "第二条")
    await fallback.add_message("session-a", "user", "第三条")
    history_a = await fallback.get_history("session-a")
    history_b = await fallback.get_history("session-b")
    results.append({
        "case": "short_term_fallback_limit_and_isolation",
        "passed": (
            len(history_a) == 2
            and history_a[0]["content"] == "第二条"
            and not history_b
        ),
        "actual": history_a,
    })

    await fallback.clear_history("session-a")
    cleared = await fallback.get_history("session-a")
    results.append({
        "case": "short_term_clear",
        "passed": not cleared,
        "actual": cleared,
    })

    # 使用默认配置验证实际运行时的 Redis 或降级路径。
    runtime = ShortTermMemory(max_turns=3)
    await runtime.add_message("runtime-session", "user", "runtime message")
    runtime_history = await runtime.get_history("runtime-session")
    results.append({
        "case": "short_term_runtime_roundtrip",
        "passed": bool(runtime_history)
        and runtime_history[-1]["content"] == "runtime message",
        "backend": "redis" if runtime.redis is not None else "fallback",
        "actual": runtime_history,
    })
    await runtime.clear_history("runtime-session")
    return results


def test_working() -> list[dict]:
    memory = WorkingMemory(max_entries=2)
    memory.update("session-a", {"step": 1, "agent": "supervisor"})
    memory.update("session-a", {"step": 2, "agent": "knowledge_rag"})
    memory.update("session-a", {"step": 3, "agent": "synthesizer"})

    context = memory.get_context("session-a")
    history = memory.get_history("session-a")
    result = [{
        "case": "working_context_and_history_limit",
        "passed": (
            context["step"] == 3
            and context["agent"] == "synthesizer"
            and len(history) == 2
            and history[0]["data"]["step"] == 2
        ),
        "actual": {"context": context, "history": history},
    }]

    memory.clear("session-a")
    result.append({
        "case": "working_clear",
        "passed": not memory.get_context("session-a")
        and not memory.get_history("session-a"),
        "actual": memory.export_for_persistence("session-a"),
    })
    return result


async def test_long_term() -> list[dict]:
    memory = LongTermMemory()
    before = len(memory.documents)
    results = await memory.search(
        "签收后几天可以申请退货？",
        top_k=3,
        min_score=0.55,
    )
    restored = LongTermMemory()
    return [
        {
            "case": "long_term_search",
            "passed": any(
                item.get("source") == "return_policy.txt"
                for item in results
            ),
            "actual": [item.get("source") for item in results],
        },
        {
            "case": "long_term_persistence",
            "passed": before > 0 and len(restored.documents) == before,
            "actual": {
                "before": before,
                "restored": len(restored.documents),
            },
        },
    ]


async def main() -> None:
    results = []
    results.extend(await test_short_term())
    results.extend(test_working())
    results.extend(await test_long_term())

    passed = sum(item["passed"] for item in results)
    summary = {
        "metric": "memory_evaluation",
        "total": len(results),
        "passed": passed,
        "accuracy_percent": round(passed / len(results) * 100, 2),
        "results": results,
    }
    output_path = Path(__file__).with_name("results") / "memory-results.json"
    output_path.parent.mkdir(exist_ok=True)
    output_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for item in results:
        print(f"{item['case']}: passed={item['passed']}")
    print(f"\n记忆测试通过率: {summary['accuracy_percent']}%")
    print(f"详细结果: {output_path}")


if __name__ == "__main__":
    asyncio.run(main())

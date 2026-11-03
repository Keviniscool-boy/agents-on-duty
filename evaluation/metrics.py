"""Pure metric helpers for the Stage 4 knowledge evaluation.

Keeping the scoring logic here makes it unit-testable without a live model,
embedding service, or running API.
"""

REFUSAL_MARKERS = ("没有相关信息", "无法确认", "暂时无法", "没有足够")
HANDOFF_MARKERS = ("人工客服", "转人工", "提交工单")


def is_refusal(answer: str) -> bool:
    return any(marker in answer for marker in REFUSAL_MARKERS)


def is_handoff(answer: str, data: dict | None = None) -> bool:
    if data and data.get("handoff_required"):
        return True
    return any(marker in answer for marker in HANDOFF_MARKERS)


def score_case(
    case: dict,
    sources: list[str],
    answer: str,
    data: dict | None = None,
) -> dict:
    """Score one evaluation case on recall, citation, refusal and handoff."""
    expected_source = case.get("expected_source")
    expected_action = case.get("expected_action", "answer")
    source_hit = (
        any(str(source).startswith(expected_source) for source in sources)
        if expected_source
        else not sources
    )
    refusal = is_refusal(answer)
    handoff = is_handoff(answer, data)
    if expected_source:
        cited = expected_source in answer or any(
            str(source) in answer
            for source in sources
            if str(source).startswith(expected_source)
        )
        cited_full = any(
            str(source) in answer
            for source in sources
            if str(source).startswith(expected_source)
        )
        citation_correct = bool(cited) and not refusal
        citation_full = bool(cited_full) and not refusal
    else:
        # 负样本：不应检索到任何来源，也不应编造引用。
        citation_correct = not sources
        citation_full = not sources
    return {
        "recall_at_k": source_hit,
        "citation_correct": citation_correct,
        # 更严格但仍非 NLI：doc_id + 版本 + 章节必须出现在答案中。
        "citation_full": citation_full,
        "refusal_correct": refusal == (expected_action == "refuse"),
        "handoff_correct": handoff == (expected_action == "handoff"),
    }

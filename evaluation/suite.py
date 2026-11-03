"""Versioned evaluation suite helpers: environment, reports, comparison."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

SUITE_VERSION = "2026.11-stage5"
EVALUATION_DIR = Path(__file__).resolve().parent
RESULTS_DIR = EVALUATION_DIR / "results"


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(EVALUATION_DIR.parent),
        )
        return result.stdout.strip()
    except Exception:  # noqa: BLE001 - best-effort metadata
        return ""


def environment() -> dict:
    """Snapshot the runtime/model configuration so results are reproducible."""
    return {
        "suite_version": SUITE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "model": os.getenv("MODEL_NAME", ""),
        "embedding_model": os.getenv("EMBEDDING_MODEL", ""),
        "embedding_provider": "dashscope" if os.getenv("DASHSCOPE_API_KEY") else "openai-compatible",
    }


def build_report(
    suite: str,
    results: list[dict],
    sample_size: int | None = None,
    extra: dict | None = None,
    dataset: dict | None = None,
    corpus: dict | None = None,
) -> dict:
    failures = [item for item in results if not item.get("passed", False)]
    return {
        "suite": suite,
        **environment(),
        "sample_size": len(results) if sample_size is None else sample_size,
        "dataset": dataset or {},
        "corpus": corpus or {},
        "results": results,
        "failures": failures,
        "summary": {"passed": len(results) - len(failures), "failed": len(failures)},
        **(extra or {}),
    }


def file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dataset_info(path: str | Path, cases: list[dict] | None = None) -> dict:
    """Fingerprint the cases actually evaluated (full file or a selected subset)."""
    target = Path(path)
    if cases is not None:
        payload = "\n".join(
            json.dumps(case, ensure_ascii=False, sort_keys=True) for case in cases
        )
        return {
            "path": target.name,
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            "cases": len(cases),
        }
    count = len([line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()])
    return {"path": target.name, "sha256": file_sha256(target), "cases": count}


def corpus_info(kb_dir: str | Path) -> dict:
    """Fingerprint every policy/document file used for retrieval."""
    root = Path(kb_dir)
    digest = hashlib.sha256()
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".md", ".txt"}
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return {"path": root.name, "sha256": digest.hexdigest(), "documents": len(files)}


def write_report(report: dict, path: str | Path | None = None) -> Path:
    target = Path(path) if path else RESULTS_DIR / f"{report['suite']}-latest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


COMPARABLE_KEYS = ("suite_version", "model", "embedding_model", "embedding_provider")


def compare(baseline: dict, candidate: dict) -> dict:
    """Only report deltas when suite, model, dataset, corpus and sample size all match."""
    reasons = [key for key in COMPARABLE_KEYS if baseline.get(key) != candidate.get(key)]
    baseline_dataset = baseline.get("dataset") or {}
    candidate_dataset = candidate.get("dataset") or {}
    baseline_corpus = baseline.get("corpus") or {}
    candidate_corpus = candidate.get("corpus") or {}

    for key, baseline_meta, candidate_meta in (
        ("dataset", baseline_dataset, candidate_dataset),
        ("corpus", baseline_corpus, candidate_corpus),
    ):
        baseline_digest = str(baseline_meta.get("sha256", ""))
        candidate_digest = str(candidate_meta.get("sha256", ""))
        if not baseline_digest or not candidate_digest:
            reasons.append(f"{key}_missing")
        elif baseline_digest != candidate_digest:
            reasons.append(key)

    baseline_cases = baseline_dataset.get("cases")
    candidate_cases = candidate_dataset.get("cases")
    if (
        not isinstance(baseline_cases, int) or baseline_cases <= 0
        or not isinstance(candidate_cases, int) or candidate_cases <= 0
    ):
        reasons.append("dataset_cases_missing")
    elif baseline_cases != candidate_cases:
        reasons.append("dataset_cases")

    baseline_size = baseline.get("sample_size")
    candidate_size = candidate.get("sample_size")
    if (
        not isinstance(baseline_size, int) or baseline_size <= 0
        or not isinstance(candidate_size, int) or candidate_size <= 0
    ):
        reasons.append("sample_size_missing")
    elif baseline_size != candidate_size:
        reasons.append("sample_size")
    elif isinstance(baseline_cases, int) and baseline_cases > 0 and baseline_size != baseline_cases:
        reasons.append("dataset_cases_vs_sample_size")

    baseline_documents = baseline_corpus.get("documents")
    candidate_documents = candidate_corpus.get("documents")
    if (
        not isinstance(baseline_documents, int) or baseline_documents <= 0
        or not isinstance(candidate_documents, int) or candidate_documents <= 0
    ):
        reasons.append("corpus_documents_missing")
    elif baseline_documents != candidate_documents:
        reasons.append("corpus_documents")

    reasons = list(dict.fromkeys(reasons))
    if reasons:
        return {"comparable": False, "reasons": reasons, "deltas": {}}
    deltas = {}
    baseline_summary = baseline.get("summary", {})
    candidate_summary = candidate.get("summary", {})
    for key, value in candidate_summary.items():
        if isinstance(value, (int, float)) and isinstance(baseline_summary.get(key), (int, float)):
            deltas[key] = round(value - baseline_summary[key], 2)
    return {"comparable": True, "reasons": [], "deltas": deltas}

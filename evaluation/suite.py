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


def dataset_info(path: str | Path) -> dict:
    """Fingerprint the evaluation cases so different datasets are not compared."""
    target = Path(path)
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


def _digest(report: dict, key: str) -> str:
    return str((report.get(key) or {}).get("sha256", ""))


def compare(baseline: dict, candidate: dict) -> dict:
    """Only report deltas when suite, model, dataset and corpus all match."""
    reasons = [key for key in COMPARABLE_KEYS if baseline.get(key) != candidate.get(key)]
    for key in ("dataset", "corpus"):
        baseline_digest, candidate_digest = _digest(baseline, key), _digest(candidate, key)
        if (baseline_digest or candidate_digest) and baseline_digest != candidate_digest:
            reasons.append(key)
    if baseline.get("sample_size") != candidate.get("sample_size"):
        reasons.append("sample_size")
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

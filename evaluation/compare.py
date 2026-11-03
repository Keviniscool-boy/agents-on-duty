"""Compare two versioned evaluation reports (only when comparable)."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evaluation.suite import compare


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two evaluation reports")
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    args = parser.parse_args()

    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    candidate = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
    result = compare(baseline, candidate)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["comparable"] else 1


if __name__ == "__main__":
    sys.exit(main())

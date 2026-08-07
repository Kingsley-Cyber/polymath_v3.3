#!/usr/bin/env python3
"""Create a stage receipt scaffold."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PACK_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage_id")
    parser.add_argument("--output")
    args = parser.parse_args()
    output = Path(args.output).resolve() if args.output else PACK_ROOT / "artifacts" / "receipts" / f"{args.stage_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    value = {
        "stage_id": args.stage_id,
        "status": "passed",
        "summary": "REPLACE WITH OBSERVABLE IMPLEMENTATION RESULT",
        "commands": [],
        "artifacts": [],
        "metrics": {},
        "facts": {},
        "notes": [],
    }
    output.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

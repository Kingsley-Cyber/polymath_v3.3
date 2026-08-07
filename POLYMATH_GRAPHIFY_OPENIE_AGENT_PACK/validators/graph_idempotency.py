from __future__ import annotations
import json
from pathlib import Path


def compare_counts(first_path: str | Path, second_path: str | Path) -> dict:
    a = json.loads(Path(first_path).read_text(encoding="utf-8"))
    b = json.loads(Path(second_path).read_text(encoding="utf-8"))
    errors = []
    for key in sorted(set(a) | set(b)):
        if a.get(key) != b.get(key):
            errors.append({"key": key, "first": a.get(key), "second": b.get(key)})
    return {"passed": not errors, "errors": errors}


if __name__ == "__main__":
    import sys
    print(json.dumps(compare_counts(sys.argv[1], sys.argv[2]), indent=2))

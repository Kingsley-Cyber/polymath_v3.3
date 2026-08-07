from __future__ import annotations
import json, re
from pathlib import Path

REQUIRED_TRACE = ["gliner2", "raw mention", "entity reducer", "mention completion", "triplet", "argument adapter", "proposition reducer", "predicate", "assertion", "graph"]
FORBIDDEN_RUNTIME = ["glirel", "relex", "rebel", "lfm", "cuda", "mps", "mlx"]


def validate_trace(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {"passed": False, "errors": [f"missing trace: {p}"]}
    text = p.read_text(encoding="utf-8", errors="ignore").lower()
    errors = []
    for term in REQUIRED_TRACE:
        if term not in text:
            errors.append(f"missing required reachability term: {term}")
    forbidden_found = [term for term in FORBIDDEN_RUNTIME if re.search(rf"\b{re.escape(term)}\b", text)]
    return {"passed": not errors and not forbidden_found, "errors": errors, "forbidden_found": forbidden_found}


if __name__ == "__main__":
    import sys
    print(json.dumps(validate_trace(sys.argv[1]), indent=2))

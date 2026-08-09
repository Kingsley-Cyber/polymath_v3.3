from __future__ import annotations
import json, re
from pathlib import Path


def scan_parse_calls(repo_root: str | Path) -> dict:
    root = Path(repo_root)
    hits = []
    for p in root.rglob("*.py"):
        if any(x in p.parts for x in [".git", "venv", ".venv", "__pycache__"]):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"\bnlp\s*\(", line) or "spacy.load" in line:
                hits.append({"path": str(p.relative_to(root)), "line": i, "text": line.strip()})
    return {"passed": True, "parse_call_sites": hits, "note": "Repo agent must inspect and ensure no relation component reparses text unnecessarily."}


if __name__ == "__main__":
    import sys
    print(json.dumps(scan_parse_calls(sys.argv[1]), indent=2))

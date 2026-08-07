from __future__ import annotations
import json
from pathlib import Path


def validate_gold_offsets(fixture_md: str | Path, gold_json: str | Path) -> dict:
    text = Path(fixture_md).read_text(encoding="utf-8")
    gold = json.loads(Path(gold_json).read_text(encoding="utf-8"))
    errors = []
    checked = 0
    for ent in gold.get("entities", []):
        start, end, surface = ent["start"], ent["end"], ent["text"]
        checked += 1
        if text[start:end] != surface:
            errors.append({"id": ent.get("id"), "expected": surface, "actual": text[start:end], "start": start, "end": end})
    return {"passed": not errors, "checked_entities": checked, "errors": errors[:20]}


if __name__ == "__main__":
    import sys
    print(json.dumps(validate_gold_offsets(sys.argv[1], sys.argv[2]), indent=2))

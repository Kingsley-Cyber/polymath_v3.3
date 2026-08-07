from __future__ import annotations
import json, re
from pathlib import Path

FORBIDDEN = ["cuda", "mps", "mlx"]


def validate_provider_config(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {"passed": False, "errors": [f"missing provider config: {p}"]}
    text = p.read_text(encoding="utf-8", errors="ignore").lower()
    errors = []
    if "cpu" not in text:
        errors.append("CPU assertion not found")
    for word in FORBIDDEN:
        if re.search(rf"device\s*[:=]\s*[\"']?{word}", text):
            errors.append(f"Forbidden device selected: {word}")
    return {"passed": not errors, "errors": errors}


if __name__ == "__main__":
    import sys
    print(json.dumps(validate_provider_config(sys.argv[1]), indent=2))

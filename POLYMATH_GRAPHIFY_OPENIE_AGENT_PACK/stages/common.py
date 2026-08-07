from __future__ import annotations
import hashlib, json, os, re
from pathlib import Path
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def scan_files(repo_root: Path, patterns):
    skip = {
        ".git", ".pytest_cache", ".cache", ".flash", "node_modules", ".venv",
        "venv", "__pycache__", "dist", "build", "work", "data", "data_eval",
        "models", "heads", "tmp",
    }
    skip_prefixes = (".venv", ".tmp", "graphify-out", "incoming_", "batch_out")
    hits = []
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [
            name for name in dirs
            if name not in skip and not name.startswith(skip_prefixes)
        ]
        root_path = Path(root)
        for filename in files:
            p = root_path / filename
            if p.suffix.lower() not in {".py", ".md", ".yaml", ".yml", ".json", ".toml", ".txt", ".sh"}:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            rel = str(p.relative_to(repo_root))
            for name, rx in patterns.items():
                if re.search(rx, text, flags=re.I):
                    hits.append({"capability": name, "path": rel, "matches": len(re.findall(rx, text, flags=re.I))})
    return hits

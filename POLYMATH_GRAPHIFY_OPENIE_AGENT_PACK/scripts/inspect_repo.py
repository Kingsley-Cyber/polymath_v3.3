#!/usr/bin/env python3
from __future__ import annotations
import json, re, argparse
from pathlib import Path
import os

PATTERNS = {
    "graphify": r"graphify|graph[_-]?ify|knowledge graph",
    "gliner": r"GLiNER|gliner",
    "glirel": r"GLiREL|glirel",
    "relex": r"Relex|relex",
    "triplet_extract": r"triplet|OpenIE|openie",
    "spacy": r"spacy\.load|nlp\(",
    "neo4j": r"Neo4j|MERGE|UNWIND",
    "mongo": r"Mongo|pymongo|motor",
    "predicate": r"predicate|ontology|relation_acceptance|endpoint",
}


def scan(repo_root: Path) -> dict:
    hits = []
    skip = {
        ".git", ".pytest_cache", ".cache", ".flash", "node_modules", ".venv",
        "venv", "__pycache__", "dist", "build", "work", "data", "data_eval",
        "models", "heads", "tmp",
    }
    skip_prefixes = (".venv", ".tmp", "graphify-out", "incoming_", "batch_out")
    for root, dirs, files in os.walk(repo_root):
        dirs[:] = [
            name for name in dirs
            if name not in skip and not name.startswith(skip_prefixes)
        ]
        root_path = Path(root)
        for filename in files:
            p = root_path / filename
            if p.suffix.lower() not in {".py", ".md", ".json", ".yaml", ".yml", ".toml", ".sh", ".txt"}:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
            for k, rx in PATTERNS.items():
                if re.search(rx, text, re.I):
                    hits.append({"kind": k, "path": str(p.relative_to(repo_root)), "count": len(re.findall(rx, text, re.I))})
    return {"repo_root": str(repo_root), "hits": hits}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo_root")
    ap.add_argument("--out")
    args = ap.parse_args()
    result = scan(Path(args.repo_root).resolve())
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)

if __name__ == "__main__":
    main()

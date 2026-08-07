#!/usr/bin/env python3
"""Create a repository-grounded discovery report without guessing attachment paths."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import re
from typing import Iterable

PACK_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PACK_ROOT / "artifacts" / "discovery"

PATTERNS = {
    "graphify": re.compile(r"\bgraphify\b", re.I),
    "gliner2": re.compile(r"\bgliner\s*2\b|gliner2", re.I),
    "ordinary_gliner": re.compile(r"\bgliner\b", re.I),
    "glirel": re.compile(r"\bglirel\b", re.I),
    "relex": re.compile(r"\brelex\b|gliner[-_ ]?relex", re.I),
    "spacy": re.compile(r"\bspacy\b|\bnlp\.pipe\b|\bnlp\s*\(", re.I),
    "dependency_matcher": re.compile(r"DependencyMatcher|dependency[_ -]?matcher", re.I),
    "frame_extractor": re.compile(r"FrameExtractor|frame[_ -]?extract", re.I),
    "svo": re.compile(r"\bSVO\b|subject[_ -]?verb[_ -]?object", re.I),
    "predicate_mapping": re.compile(r"predicate[_ -]?(map|synonym|normal)|canonical[_ -]?predicate", re.I),
    "endpoint_signature": re.compile(r"endpoint[_ -]?signature|type[_ -]?pair", re.I),
    "mongo": re.compile(r"\bmongo(db)?\b|motor\.", re.I),
    "neo4j": re.compile(r"\bneo4j\b|GraphDatabase", re.I),
    "graph_write": re.compile(r"graph[_ -]?write|MERGE\s*\(|UNWIND", re.I),
    "control_plane": re.compile(r"control[_ -]?plane|stage[_ -]?attempt|run[_ -]?ledger", re.I),
    "acceptance_gate": re.compile(r"accept[_ -]?(corroborated|syntax|relex)|corroboration[_ -]?gate|REVIEW_CONFLICT", re.I),
}

EXTENSIONS = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".sh", ".ini", ".cfg"}
IGNORE_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__", ".mypy_cache", ".pytest_cache"}


def iter_files(repo_root: Path) -> Iterable[Path]:
    pack_resolved = PACK_ROOT.resolve()
    for path in repo_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            continue
        if any(part in IGNORE_DIRS for part in path.parts):
            continue
        try:
            if path.resolve().is_relative_to(pack_resolved):
                continue
        except AttributeError:
            if str(path.resolve()).startswith(str(pack_resolved)):
                continue
        try:
            if path.stat().st_size > 5_000_000:
                continue
        except OSError:
            continue
        yield path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--max-hits-per-pattern", type=int, default=250)
    args = parser.parse_args()

    repo_root = Path(args.repo_root).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    hits: dict[str, list[dict[str, object]]] = {name: [] for name in PATTERNS}
    file_scores: dict[str, int] = {}
    scanned = 0

    for path in iter_files(repo_root):
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(repo_root))
        lines = text.splitlines()
        matched_names: set[str] = set()
        for line_no, line in enumerate(lines, 1):
            for name, pattern in PATTERNS.items():
                if len(hits[name]) >= args.max_hits_per_pattern:
                    continue
                if pattern.search(line):
                    hits[name].append({"path": rel, "line": line_no, "text": line.strip()[:500]})
                    matched_names.add(name)
        if matched_names:
            file_scores[rel] = len(matched_names)

    ranked_files = [
        {"path": path, "matched_categories": score}
        for path, score in sorted(file_scores.items(), key=lambda item: (-item[1], item[0]))
    ]
    report = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "repo_root": str(repo_root),
        "files_scanned": scanned,
        "patterns": {name: len(values) for name, values in hits.items()},
        "ranked_files": ranked_files,
        "hits": hits,
        "warning": "This is a search aid. The agent must inspect and confirm actual ownership and runtime attachment points.",
    }
    json_path = output_dir / "repo_probe.json"
    md_path = output_dir / "repo_probe.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Repository Probe",
        "",
        f"- Repository: `{repo_root}`",
        f"- Files scanned: {scanned}",
        "",
        "## Highest-signal files",
        "",
    ]
    for item in ranked_files[:80]:
        lines.append(f"- `{item['path']}` — {item['matched_categories']} categories")
    lines.extend(["", "## Pattern counts", ""])
    for name, count in report["patterns"].items():
        lines.append(f"- `{name}`: {count}")
    lines.extend(["", "## Required manual confirmation", "", "Confirm the canonical entrypoint, provider ownership, parse owner, relation consumers, gates, stores, and test seams before editing."])
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

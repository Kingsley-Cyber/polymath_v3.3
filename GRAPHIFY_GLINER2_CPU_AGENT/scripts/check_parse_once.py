#!/usr/bin/env python3
"""Check that spaCy construction/parsing is owned centrally and relation consumers reuse Doc objects."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

PACK_ROOT = Path(__file__).resolve().parents[1]


def call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = [func.attr]
        value = func.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            parts.append(value.id)
        return ".".join(reversed(parts))
    return ""


def scan(path: Path) -> dict[str, list[dict[str, object]]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        return {"syntax_errors": [{"line": exc.lineno, "message": exc.msg}], "calls": []}
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = call_name(node)
            if name:
                calls.append({"name": name, "line": getattr(node, "lineno", None)})
    return {"syntax_errors": [], "calls": calls}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--scope", default=str(PACK_ROOT / ".agent_state" / "extraction_scope.json"))
    parser.add_argument("--output", default=str(PACK_ROOT / "artifacts" / "reports" / "parse_once_policy.json"))
    args = parser.parse_args()

    repo_root = Path(args.repo_root).expanduser().resolve()
    scope = json.loads(Path(args.scope).read_text(encoding="utf-8"))
    owner_files = scope.get("spacy_owner_files", [])
    consumer_files = scope.get("relation_consumer_files", [])
    if not owner_files or not consumer_files:
        raise SystemExit("Scope must include spacy_owner_files and relation_consumer_files")

    report = {"owners": {}, "consumers": {}, "violations": [], "passed": True}
    owner_has_pipe = False
    for rel in owner_files:
        path = repo_root / rel
        if not path.is_file():
            report["violations"].append({"file": rel, "rule": "missing_owner_file"})
            continue
        data = scan(path)
        report["owners"][rel] = data
        names = [item["name"] for item in data["calls"]]
        owner_has_pipe = owner_has_pipe or any(name.endswith(".pipe") for name in names)
    if not owner_has_pipe:
        report["violations"].append({"rule": "no_nlp_pipe_in_owner_files"})

    for rel in consumer_files:
        path = repo_root / rel
        if not path.is_file():
            report["violations"].append({"file": rel, "rule": "missing_consumer_file"})
            continue
        data = scan(path)
        report["consumers"][rel] = data
        for item in data["calls"]:
            name = str(item["name"])
            if name == "spacy.load" or name == "nlp" or name.endswith(".nlp") or name.endswith(".pipe"):
                report["violations"].append({"file": rel, "line": item["line"], "rule": "consumer_parses_text", "call": name})

    report["passed"] = not report["violations"]
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(output)
    if not report["passed"]:
        print(json.dumps(report, indent=2))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

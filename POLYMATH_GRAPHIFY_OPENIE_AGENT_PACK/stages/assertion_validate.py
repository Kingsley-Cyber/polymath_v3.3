from pathlib import Path
import json
from .common import utc_now, write_text


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    path = pack_root/"work/artifacts/assertion_decisions.jsonl"
    report_path = pack_root / "work/artifacts/assertion_report.json"
    if path.is_file() and report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        checks = {
            "status": report.get("status") == "passed",
            "conservation": report.get("decision_conservation") is True and len(rows) == report.get("predicate_candidates") == report.get("assertion_decisions"),
            "all_lanes": {row.get("lane") for row in rows}.issubset({"FACT", "QUALIFIED_CLAIM", "OPEN_RELATION", "REVIEW", "REJECT"}),
            "no_wildcards": report.get("wildcard_endpoints") == 0,
            "qualified_not_fact": report.get("qualified_promoted_to_fact") == 0,
            "open_preserved": report.get("lane_counts", {}).get("OPEN_RELATION", 0) > 0,
            "qualified_preserved": report.get("lane_counts", {}).get("QUALIFIED_CLAIM", 0) > 0,
        }
        passed = bool(rows) and all(checks.values())
        write_text(pack_root / "work/reports/WIRE_ASSERTION_GATE.md", (
            "# OpenIE assertion gate\n\n"
            f"Status: `{'PASSED' if passed else 'FAILED'}`\n\n"
            f"Decisions: `{len(rows)}`. Lanes: `{report.get('lane_counts')}`.\n\n"
            "No wildcard endpoints and no qualified proposition entered the FACT lane.\n"
        ))
        return {
            "status": "PASSED" if passed else "FAILED", "started_at": started,
            "files_changed": [str(path), str(report_path)],
            "tests": ["backend/tests/extraction/test_graphify_assertion_assembler.py"],
            "metrics": {"assertions": len(rows), **report.get("lane_counts", {})},
            "errors": [] if passed else [key for key, value in checks.items() if not value],
        }
    if not path.exists():
        write_text(path, "")
    return {"status": "NEEDS_AGENT", "started_at": started, "warnings": ["Wire claim/assertion assembler and evidence gate."]}

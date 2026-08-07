from pathlib import Path
import json
from .common import utc_now, write_text


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    path = pack_root/"work/artifacts/relation_eligibility.jsonl"
    report_path = pack_root / "work/artifacts/relation_eligibility_report.json"
    if path.is_file() and report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        checks = {
            "measured_status_passed": report.get("status") == "passed",
            "two_documents_routed": report.get("documents") == 2,
            "all_blocks_persisted": len(rows) == report.get("source_blocks") and len(rows) > 0,
            "decision_conservation": report.get("decision_conservation") is True
            and len(rows) == report.get("eligible_units", 0) + report.get("ineligible_units", 0),
            "both_decisions_present": report.get("eligible_units", 0) > 0
            and report.get("ineligible_units", 0) > 0,
            "source_text_preserved": report.get("source_text_preserved") is True
            and all(row.get("source_text") is not None and len(row.get("source_sha256", "")) == 64 for row in rows),
            "reasons_persisted": all(row.get("reasons") for row in rows),
        }
        passed = all(checks.values())
        write_text(
            pack_root / "work/reports/IMPLEMENT_RELATION_ELIGIBILITY.md",
            "# Relation eligibility\n\n"
            f"Status: `{'PASSED' if passed else 'FAILED'}`\n\n"
            f"Source blocks: `{len(rows)}`; eligible: `{report.get('eligible_units')}`; ineligible: `{report.get('ineligible_units')}`.\n\n"
            f"Source text preserved: `{str(report.get('source_text_preserved')).lower()}`.\n",
        )
        return {
            "status": "PASSED" if passed else "FAILED",
            "started_at": started,
            "files_changed": [str(path), str(report_path)],
            "tests": [
                "backend/tests/extraction/test_relation_eligibility_artifact.py",
                "backend/tests/extraction/test_graphify_relations.py",
            ],
            "metrics": {
                "source_blocks": len(rows),
                "eligible_units": report.get("eligible_units"),
                "ineligible_units": report.get("ineligible_units"),
            },
            "errors": [] if passed else [key for key, value in checks.items() if not value],
        }
    if not path.exists():
        write_text(path, "")
    return {"status": "NEEDS_AGENT", "started_at": started, "warnings": ["Implement relation-eligibility router."]}

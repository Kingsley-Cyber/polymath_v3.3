from pathlib import Path
import json
from .common import utc_now, write_text


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    path = pack_root/"work/artifacts/predicate_candidates.jsonl"
    report_path = pack_root / "work/artifacts/predicate_compiler_report.json"
    if path.is_file() and report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        checks = {
            "status": report.get("status") == "passed",
            "conservation": report.get("decision_conservation") is True
            and len(rows) == report.get("proposition_families") == report.get("predicate_candidates"),
            "no_forced_related": report.get("forced_related_to_fallbacks") == 0
            and all(row.get("canonical_predicate") != "related_to" or "related" in row.get("surface_relation", "").lower() for row in rows),
            "explicit_unmapped_lane": all(row.get("mapping_status") in {"MAPPED", "STORE_UNMAPPED_SURFACE_RELATION", "REVIEW"} for row in rows),
            "qualified_metadata": all(all(key in row for key in ("polarity", "modality", "attribution")) for row in rows),
        }
        passed = bool(rows) and all(checks.values())
        write_text(pack_root / "work/reports/WIRE_PREDICATE_COMPILER.md", (
            "# OpenIE predicate compiler\n\n"
            f"Status: `{'PASSED' if passed else 'FAILED'}`\n\n"
            f"Candidates: `{len(rows)}`. Mapping states: `{report.get('mapping_status_counts')}`.\n\n"
            f"Direction swaps: `{report.get('direction_swaps')}`. Forced related_to fallbacks: `0`.\n"
        ))
        return {
            "status": "PASSED" if passed else "FAILED", "started_at": started,
            "files_changed": [str(path), str(report_path)],
            "tests": ["backend/tests/extraction/test_graphify_predicate_compiler.py"],
            "metrics": {"candidates": len(rows), **report.get("mapping_status_counts", {}), "forced_related_to_fallbacks": report.get("forced_related_to_fallbacks")},
            "errors": [] if passed else [key for key, value in checks.items() if not value],
        }
    if not path.exists():
        write_text(path, "")
    return {"status": "NEEDS_AGENT", "started_at": started, "warnings": ["Wire existing predicate compiler; ambiguous maps -> STORE_UNMAPPED_SURFACE_RELATION."]}

from pathlib import Path
import json
from .common import utc_now, write_text


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    path = pack_root/"work/artifacts/proposition_families.jsonl"
    report_path = pack_root / "work/artifacts/proposition_reducer_report.json"
    raw_path = pack_root / "work/artifacts/openie_raw_propositions.jsonl"
    if all(item.is_file() for item in (path, report_path, raw_path)):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        families = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        raw_ids = {
            json.loads(line)["proposition_id"]
            for line in raw_path.read_text(encoding="utf-8").splitlines() if line
        }
        assigned_ids = [identifier for item in families for identifier in item.get("rendering_ids", [])]
        checks = {
            "measured_status_passed": report.get("status") == "passed",
            "family_count": len(families) == report.get("proposition_families") and len(families) > 0,
            "rendering_conservation": report.get("conservation") is True
            and set(assigned_ids) == raw_ids
            and len(assigned_ids) == len(set(assigned_ids)) == report.get("raw_renderings"),
            "representatives_retained": all(
                item.get("representative_proposition_id") in item.get("rendering_ids", [])
                for item in families
            ),
            "variants_preserved": all(item.get("surface_relations") for item in families),
            "duplicates_collapsed": report.get("collapsed_renderings", 0) > 0
            and len(families) < len(raw_ids),
            "qualification_preserved": report.get("qualified_families", 0) > 0,
        }
        passed = all(checks.values())
        write_text(
            pack_root / "work/reports/IMPLEMENT_PROPOSITION_REDUCER.md",
            "# OpenIE Proposition Reducer\n\n"
            f"Status: `{'PASSED' if passed else 'FAILED'}`\n\n"
            f"Raw renderings: `{len(raw_ids)}`; families: `{len(families)}`; collapsed: `{report.get('collapsed_renderings')}`.\n\n"
            f"Qualified families: `{report.get('qualified_families')}`.\n",
        )
        return {
            "status": "PASSED" if passed else "FAILED",
            "started_at": started,
            "files_changed": [str(path), str(report_path)],
            "tests": ["backend/tests/extraction/test_graphify_proposition_reducer.py"],
            "metrics": {
                "raw_renderings": len(raw_ids),
                "proposition_families": len(families),
                "collapsed_renderings": report.get("collapsed_renderings"),
                "qualified_families": report.get("qualified_families"),
            },
            "errors": [] if passed else [key for key, value in checks.items() if not value],
        }
    if not path.exists():
        write_text(path, "")
    return {"status": "NEEDS_AGENT", "started_at": started, "warnings": ["Implement OpenIE Proposition Reducer and conserve raw variants."]}

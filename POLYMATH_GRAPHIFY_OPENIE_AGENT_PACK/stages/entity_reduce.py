from pathlib import Path
import json
from .common import utc_now, write_text


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    path = pack_root/"work/artifacts/document_entities.jsonl"
    assignments_path = pack_root / "work/artifacts/mention_assignments.jsonl"
    report_path = pack_root / "work/artifacts/reducer_report.json"
    raw_path = pack_root / "work/artifacts/raw_mentions.jsonl"
    if all(item.is_file() for item in (path, assignments_path, report_path, raw_path)):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        entities = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        assignments = [json.loads(line) for line in assignments_path.read_text(encoding="utf-8").splitlines() if line]
        raw = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines() if line]
        aligned_ids = {
            item["mention_id"] for item in raw if item.get("terminal_state") == "aligned"
        }
        assigned_ids = [item["mention_id"] for item in assignments]
        terminal_states = {"promoted", "document_local", "review", "suppressed"}
        checks = {
            "measured_status_passed": report.get("status") == "passed",
            "two_documents_reduced": report.get("documents") == 2,
            "conservation": report.get("conservation") is True
            and report.get("aligned_raw_mentions") == report.get("terminal_assignments")
            == len(assignments),
            "every_aligned_mention_once": set(assigned_ids) == aligned_ids
            and len(assigned_ids) == len(set(assigned_ids)),
            "entities_persisted": len(entities) == report.get("clusters") and len(entities) > 0,
            "terminal_state_total": sum(report.get("state_counts", {}).values()) == len(entities),
            "terminal_states_valid": all(item.get("state") in terminal_states for item in entities),
            "no_hard_cap": report.get("hard_entity_cap") is None,
            "identity_digest": len(str(report.get("identity_digest") or "")) == 64,
        }
        passed = all(checks.values())
        write_text(
            pack_root / "work/reports/IMPLEMENT_ENTITY_REDUCER.md",
            "# Entity reducer\n\n"
            f"Status: `{'PASSED' if passed else 'FAILED'}`\n\n"
            f"Aligned mentions and assignments: `{len(assignments)}`.\n\n"
            f"Document entity clusters: `{len(entities)}`.\n\n"
            f"Terminal state counts: `{json.dumps(report.get('state_counts', {}), sort_keys=True)}`.\n",
        )
        return {
            "status": "PASSED" if passed else "FAILED",
            "started_at": started,
            "files_changed": [str(item) for item in (path, assignments_path, report_path)],
            "tests": ["backend/tests/extraction/test_graphify_reducer.py"],
            "metrics": {
                "aligned_raw_mentions": report.get("aligned_raw_mentions"),
                "terminal_assignments": report.get("terminal_assignments"),
                "clusters": report.get("clusters"),
                "state_counts": report.get("state_counts"),
            },
            "errors": [] if passed else [key for key, value in checks.items() if not value],
        }
    if not path.exists():
        write_text(path, "")
    return {"status": "NEEDS_AGENT", "started_at": started, "warnings": ["Implement mention -> document entity reducer in target repo."]}

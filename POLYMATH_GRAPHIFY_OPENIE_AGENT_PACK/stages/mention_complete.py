from pathlib import Path
import json
from .common import utc_now, write_text


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    path = pack_root/"work/artifacts/completed_mentions.jsonl"
    report_path = pack_root / "work/artifacts/completion_report.json"
    if path.is_file() and report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        mentions = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        checks = {
            "measured_status_passed": report.get("status") == "passed",
            "two_documents_completed": report.get("documents") == 2,
            "mentions_persisted": len(mentions) == report.get("completed_mentions") and len(mentions) > 0,
            "strict_offsets": report.get("strict_offset_rate") == 1.0
            and all(item.get("normalized_end", 0) > item.get("normalized_start", -1) for item in mentions)
            and all(item.get("original_end", 0) > item.get("original_start", -1) for item in mentions),
            "deterministic_ids": report.get("deterministic_ids") is True
            and len({item["mention_id"] for item in mentions}) == len(mentions),
            "no_pronoun_endpoints": report.get("quality", {}).get("accepted_pronoun_endpoints") == 0,
            "identity_digest": len(str(report.get("identity_digest") or "")) == 64,
        }
        passed = all(checks.values())
        quality = report.get("quality", {})
        write_text(
            pack_root / "work/reports/IMPLEMENT_MENTION_COMPLETION.md",
            "# Mention completion\n\n"
            f"Status: `{'PASSED' if passed else 'FAILED'}`\n\n"
            f"Completed mentions: `{len(mentions)}`; strict offset rate: `{report.get('strict_offset_rate')}`.\n\n"
            f"Current quality exact-span F1: `{quality.get('exact_span_f1', 0.0):.6f}`; type F1: `{quality.get('type_f1', 0.0):.6f}`.\n",
        )
        return {
            "status": "PASSED" if passed else "FAILED",
            "started_at": started,
            "files_changed": [str(path), str(report_path)],
            "tests": ["backend/tests/extraction/test_graphify_completion.py"],
            "metrics": {
                "completed_mentions": len(mentions),
                "strict_offset_rate": report.get("strict_offset_rate"),
                "exact_span_f1": quality.get("exact_span_f1"),
                "type_f1": quality.get("type_f1"),
            },
            "errors": [] if passed else [key for key, value in checks.items() if not value],
        }
    if not path.exists():
        write_text(path, "")
    return {"status": "NEEDS_AGENT", "started_at": started, "warnings": ["Implement document-local PhraseMatcher/EntityRuler mention completion."]}

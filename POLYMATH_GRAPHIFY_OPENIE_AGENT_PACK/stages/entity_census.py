from pathlib import Path
import json
from .common import utc_now, write_text


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    path = pack_root/"work/artifacts/raw_mentions.jsonl"
    report_path = pack_root / "work/artifacts/census_report.json"
    offset_path = pack_root / "work/artifacts/offset_validation.json"
    windows_path = pack_root / "work/artifacts/windows.jsonl"
    if all(item.is_file() for item in (path, report_path, offset_path, windows_path)):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        offsets = json.loads(offset_path.read_text(encoding="utf-8"))
        mention_lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
        window_lines = [line for line in windows_path.read_text(encoding="utf-8").splitlines() if line]
        checks = {
            "measured_status_passed": report.get("status") == "passed",
            "two_documents_batched": report.get("documents") == 2,
            "raw_mentions_persisted": len(mention_lines) == report.get("persisted_records")
            == report.get("emitted_predictions") and len(mention_lines) > 0,
            "windows_persisted": len(window_lines) == report.get("windows") and len(window_lines) > 0,
            "mention_conservation": report.get("conservation") is True
            and report.get("emitted_predictions")
            == report.get("aligned_mentions", 0) + report.get("alignment_failures", 0),
            "strict_alignment": report.get("strict_alignment_rate") == 1.0
            and offsets.get("strict_alignment_rate") == 1.0,
            "deterministic_order": report.get("deterministic_output_order") is True,
            "batched_persistence": report.get("persistence_batches", 0) > 0,
        }
        passed = all(checks.values())
        write_text(
            pack_root / "work/reports/IMPLEMENT_ENTITY_CENSUS.md",
            "# Entity census\n\n"
            f"Status: `{'PASSED' if passed else 'FAILED'}`\n\n"
            f"Documents: `{report.get('documents')}`; windows: `{report.get('windows')}`.\n\n"
            f"Raw mentions: `{report.get('emitted_predictions')}`; aligned: `{report.get('aligned_mentions')}`; failures: `{report.get('alignment_failures')}`.\n\n"
            f"Inference seconds: `{report.get('inference_seconds', 0.0):.6f}`.\n",
        )
        return {
            "status": "PASSED" if passed else "FAILED",
            "started_at": started,
            "files_changed": [str(item) for item in (path, windows_path, report_path, offset_path)],
            "tests": ["backend/tests/extraction/test_graphify_census.py"],
            "metrics": {
                "documents": report.get("documents"),
                "windows": report.get("windows"),
                "raw_mentions": report.get("emitted_predictions"),
                "strict_alignment_rate": report.get("strict_alignment_rate"),
                "inference_seconds": report.get("inference_seconds"),
            },
            "errors": [] if passed else [key for key, value in checks.items() if not value],
        }
    if not path.exists():
        write_text(path, "")
    return {"status": "NEEDS_AGENT", "started_at": started, "warnings": ["Implement GLiNER2 raw mention census in target repo; placeholder file created."]}

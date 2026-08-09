from pathlib import Path
import json
from .common import utc_now, write_json, write_text


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    reports = {
        "quality": pack_root / "work/baseline/relex_quality.json",
        "throughput": pack_root / "work/baseline/relex_throughput.json",
    }
    if all(path.is_file() for path in reports.values()):
        measured = {name: json.loads(path.read_text(encoding="utf-8")) for name, path in reports.items()}
        record_paths = {
            name: path.with_name(f"{path.stem}_records.json") for name, path in reports.items()
        }
        checks = {
            "reports_passed": all(row.get("status") == "passed" for row in measured.values()),
            "frozen_provider": all(row.get("provider") == "relex_local_frozen_baseline" for row in measured.values()),
            "isolated_no_database_writes": all(
                row.get("checks", {}).get("isolated_no_database_writes") is True
                for row in measured.values()
            ),
            "raw_records_preserved": all(path.is_file() for path in record_paths.values()),
            "fixture_hashes_match": all(
                row.get("release_pins", {}).get("fixture_sha256")
                for row in measured.values()
            ),
        }
        passed = all(checks.values())
        manifest = {
            "status": "PASSED" if passed else "FAILED",
            "provider": "relex_local_frozen_baseline",
            "reports": {name: str(path) for name, path in reports.items()},
            "raw_records": {name: str(path) for name, path in record_paths.items()},
            "checks": checks,
            "measured": {
                name: {
                    "counts": row.get("counts", {}),
                    "metrics": row.get("metrics", {}),
                    "stage_timings": row.get("stage_timings", {}),
                    "identity_digest": row.get("identity_digest"),
                    "projection_digest": row.get("projection_digest"),
                    "release_pins": row.get("release_pins", {}),
                }
                for name, row in measured.items()
            },
            "created_at": utc_now(),
        }
        write_json(pack_root / "work/baseline/relex_baseline_manifest.json", manifest)
        write_text(
            pack_root / "work/reports/CAPTURE_BASELINE.md",
            "# Capture baseline\n\n"
            f"Status: `{manifest['status']}`\n\n"
            f"Quality windows: `{measured['quality']['counts']['windows']}`; "
            f"wall seconds: `{measured['quality']['stage_timings']['total_seconds']:.6f}`.\n\n"
            f"Throughput windows: `{measured['throughput']['counts']['windows']}`; "
            f"wall seconds: `{measured['throughput']['stage_timings']['total_seconds']:.6f}`.\n",
        )
        return {
            "status": "PASSED" if passed else "FAILED",
            "started_at": started,
            "files_changed": [str(path) for path in [*reports.values(), *record_paths.values()]],
            "tests": ["backend/tests/test_relex_fixture_baseline.py"],
            "metrics": {
                "quality_seconds": measured["quality"]["stage_timings"]["total_seconds"],
                "throughput_seconds": measured["throughput"]["stage_timings"]["total_seconds"],
            },
            "errors": [] if passed else [key for key, value in checks.items() if not value],
        }
    manifest = {
        "status": "NEEDS_AGENT",
        "instruction": "Discover current executable Relex/Graphify baseline, run it on fixtures, save entities/relations/claims/graph/timing artifacts here.",
        "required_outputs": [
            "stage_timings", "raw_entities", "accepted_entities", "relation_candidates", "final_triples",
            "qualified_claims", "rejected_records", "graph_counts", "evidence_offsets", "config_hashes"
        ],
        "created_at": utc_now(),
    }
    write_json(pack_root/"work/baseline/relex_baseline_manifest.json", manifest)
    write_text(pack_root/"work/reports/CAPTURE_BASELINE.md", "# Capture baseline\n\nRun the existing Graphify/Relex path on both fixtures and replace this placeholder with measured artifacts.\n")
    return {"status": "NEEDS_AGENT", "started_at": started, "warnings": ["Baseline capture requires executing the target repository Graphify baseline."]}

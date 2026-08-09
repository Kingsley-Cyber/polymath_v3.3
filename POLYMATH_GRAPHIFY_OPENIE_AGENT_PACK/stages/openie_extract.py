from pathlib import Path
import json
from .common import utc_now, write_text


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    path = pack_root/"work/artifacts/openie_raw_propositions.jsonl"
    report_path = pack_root / "work/artifacts/openie_report.json"
    if path.is_file() and report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        propositions = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        health = report.get("health", {})
        checks = {
            "measured_status_passed": report.get("status") == "passed",
            "two_documents": report.get("documents") == 2,
            "raw_conservation": report.get("conservation") is True
            and len(propositions) == report.get("raw_renderings")
            == report.get("persisted_propositions"),
            "balanced_cpu": health.get("device") == "cpu"
            and health.get("speed_preset") == "balanced"
            and health.get("deep_search") is False,
            "source_pinned": health.get("package_version") == "0.5.0"
            and len(str(health.get("source_commit") or "")) == 40,
            "single_warm_extractor": health.get("extractor_load_count") == 1,
            "one_call_per_unit": report.get("at_most_one_provider_call_per_eligible_unit") is True
            and report.get("every_eligible_unit_routed") is True,
            "evidence_aligned": report.get("exact_evidence_alignment") is True,
            "no_graph_writes": report.get("no_graph_writes") is True,
            "attribution_preserved": report.get("attributed_renderings", 0) > 0
            and all("asserter_chain" in item and "asserter_links" in item for item in propositions),
        }
        passed = all(checks.values())
        write_text(
            pack_root / "work/reports/INTEGRATE_TRIPLET_EXTRACT.md",
            "# triplet-extract integration\n\n"
            f"Status: `{'PASSED' if passed else 'FAILED'}`\n\n"
            f"Eligible units and calls: `{report.get('eligible_units')}`.\n\n"
            f"Persisted raw renderings: `{len(propositions)}`; attributed: `{report.get('attributed_renderings')}`.\n\n"
            f"Elapsed seconds: `{report.get('elapsed_seconds', 0.0):.6f}`.\n",
        )
        return {
            "status": "PASSED" if passed else "FAILED",
            "started_at": started,
            "files_changed": [str(path), str(report_path)],
            "tests": [
                "backend/tests/extraction/test_graphify_openie.py",
                "triplet-extract attribution and polarity tests",
            ],
            "metrics": {
                "eligible_units": report.get("eligible_units"),
                "raw_renderings": len(propositions),
                "attributed_renderings": report.get("attributed_renderings"),
                "elapsed_seconds": report.get("elapsed_seconds"),
            },
            "errors": [] if passed else [key for key, value in checks.items() if not value],
        }
    if not path.exists():
        write_text(path, "")
    return {"status": "NEEDS_AGENT", "started_at": started, "warnings": ["Integrate triplet-extract Balanced CPU and persist raw OpenIE propositions."]}

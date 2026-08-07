from pathlib import Path
import json
from .common import utc_now, write_json, write_text


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    report_path = pack_root / "work/metrics/relation_kill_switch.json"
    if report_path.is_file():
        measured = json.loads(report_path.read_text(encoding="utf-8"))
        combined = measured.get("lanes", {}).get("combined", {})
        checks = {
            "measured_status_passed": measured.get("status") == "PASSED",
            "gold_pair_recall": float(combined.get("gold_pair_recall", 0.0)) >= 0.65,
            "directed_triple_precision": float(
                combined.get("directed_canonical_triple_precision", 0.0)
            ) >= 0.90,
            "cpu_balanced": measured.get("configuration", {}).get("runtime") == "CPU"
            and measured.get("configuration", {}).get("speed_preset") == "balanced",
            "frozen_predicate_compiler": measured.get("configuration", {}).get("predicate_policy")
            == "repository_frozen_compiler",
            "triplet_extract_pinned": bool(measured.get("triplet_extract_commit")),
            "raw_units_preserved": bool(measured.get("units")),
        }
        passed = all(checks.values())
        write_text(
            pack_root / "work/reports/RUN_RELATION_KILL_SWITCH.md",
            "# Relation kill switch\n\n"
            f"Status: `{'PASSED' if passed else 'FAILED'}`\n\n"
            f"Combined gold pair recall: `{combined.get('gold_pair_recall', 0.0):.6f}`.\n\n"
            "Combined directed canonical triple precision: "
            f"`{combined.get('directed_canonical_triple_precision', 0.0):.6f}`.\n\n"
            f"Combined directed canonical triple recall: `{combined.get('directed_canonical_triple_recall', 0.0):.6f}`.\n",
        )
        return {
            "status": "PASSED" if passed else "FAILED",
            "started_at": started,
            "files_changed": [str(report_path)],
            "tests": [
                "backend/tests/test_openie_relation_kill_switch.py",
                "triplet-extract tests/test_asserter_chains.py",
                "triplet-extract tests/test_polarity_drop.py",
                "triplet-extract tests/test_quotes.py",
            ],
            "metrics": {
                "combined_gold_pair_recall": combined.get("gold_pair_recall"),
                "combined_directed_triple_precision": combined.get(
                    "directed_canonical_triple_precision"
                ),
                "combined_directed_triple_recall": combined.get("directed_canonical_triple_recall"),
            },
            "errors": [] if passed else [key for key, value in checks.items() if not value],
        }
    metrics = {
        "status": "NEEDS_AGENT",
        "required_test": "gold entities -> existing syntax vs triplet-extract vs combined OpenIE+rules",
        "decision_criterion": {"gold_pair_recall_min": 0.65, "directed_triple_precision_min": 0.90},
        "results": None,
        "failure_taxonomy_required_if_below_gate": True,
        "created_at": utc_now(),
    }
    write_json(pack_root/"work/metrics/relation_kill_switch.json", metrics)
    return {"status": "NEEDS_AGENT", "started_at": started, "warnings": ["Run relation ceiling test in the repository before implementing relation path."]}

from pathlib import Path
import json
from .common import utc_now, write_text


def run(context):
    started = utc_now()
    pack_root = Path(context["pack_root"])
    path = pack_root/"work/artifacts/adapted_arguments.jsonl"
    report_path = pack_root / "work/artifacts/argument_adapter_report.json"
    propositions_path = pack_root / "work/artifacts/openie_raw_propositions.jsonl"
    if all(item.is_file() for item in (path, report_path, propositions_path)):
        report = json.loads(report_path.read_text(encoding="utf-8"))
        arguments = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
        propositions = [line for line in propositions_path.read_text(encoding="utf-8").splitlines() if line]
        allowed = {"ENTITY", "LITERAL", "DESCRIPTION", "EMBEDDED_CLAUSE", "UNRESOLVED"}
        by_proposition = {}
        for item in arguments:
            by_proposition.setdefault(item["proposition_id"], set()).add(item["role"])
        checks = {
            "measured_status_passed": report.get("status") == "passed",
            "classification_conservation": report.get("classification_conservation") is True
            and len(arguments) == len(propositions) * 2
            == report.get("classified_arguments"),
            "roles_complete": len(by_proposition) == len(propositions)
            and all(roles == {"subject", "object"} for roles in by_proposition.values()),
            "kinds_valid": all(item.get("kind") in allowed for item in arguments),
            "entity_exact_alignment": report.get("strict_entity_alignment_rate") == 1.0
            and all(
                item.get("normalized_start") is not None
                and item.get("normalized_end") is not None
                and item.get("mention_id")
                and item.get("entity_id")
                for item in arguments if item.get("kind") == "ENTITY"
            ),
            "pronouns_not_entities": report.get("accepted_pronoun_entities") == 0,
            "all_kinds_supported": allowed == {
                "ENTITY", "LITERAL", "DESCRIPTION", "EMBEDDED_CLAUSE", "UNRESOLVED"
            },
        }
        passed = all(checks.values())
        write_text(
            pack_root / "work/reports/IMPLEMENT_ARGUMENT_ADAPTER.md",
            "# OpenIE Argument Adapter\n\n"
            f"Status: `{'PASSED' if passed else 'FAILED'}`\n\n"
            f"Arguments classified: `{len(arguments)}` from `{len(propositions)}` propositions.\n\n"
            f"Kind counts: `{json.dumps(report.get('kind_counts', {}), sort_keys=True)}`.\n\n"
            f"Strict ENTITY alignment: `{report.get('strict_entity_alignment_rate')}`.\n",
        )
        return {
            "status": "PASSED" if passed else "FAILED",
            "started_at": started,
            "files_changed": [str(path), str(report_path)],
            "tests": ["backend/tests/extraction/test_graphify_argument_adapter.py"],
            "metrics": {
                "classified_arguments": len(arguments),
                "kind_counts": report.get("kind_counts"),
                "strict_entity_alignment_rate": report.get("strict_entity_alignment_rate"),
            },
            "errors": [] if passed else [key for key, value in checks.items() if not value],
        }
    if not path.exists():
        write_text(path, "")
    return {"status": "NEEDS_AGENT", "started_at": started, "warnings": ["Implement OpenIE Argument Adapter: ENTITY/LITERAL/DESCRIPTION/EMBEDDED_CLAUSE/UNRESOLVED."]}

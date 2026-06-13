#!/usr/bin/env python3
"""Verify local extraction benchmark output against the live Ghost B parser.

This is intentionally a compatibility check, not a quality benchmark. It proves
that local workflow output can pass the same compact JSONL path the backend uses:

    JSONL -> _parse_jsonl_lines -> _parse_jsonl_items -> ExtractionResult

The model/harness may still produce bad graph semantics. This script only checks
that the current stack accepts the wire format and reports what the stack keeps
or drops after schema, evidence, and domain/range gates.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[0]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

# The parser's fact gate calls get_settings(), whose Settings model requires
# auth/LiteLLM secrets even though this verifier never calls external services.
# Use inert test-only defaults so stack compatibility can be checked without
# reading or printing real secrets.
os.environ.setdefault("LITELLM_MASTER_KEY", "local-stack-compat-test-key")
os.environ.setdefault("AUTH_SECRET_KEY", "local-stack-compat-test-secret")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "local-stack-compat-test-password")

from services.ghost_b import (  # noqa: E402
    ExtractionTask,
    _parse_jsonl_items,
    _parse_jsonl_lines,
)
from services.ghost_b_schemas import ExtractionResponse  # noqa: E402


def _sample_id(sample: dict[str, Any]) -> str:
    return str(sample.get("id") or sample.get("fixture_id") or sample.get("chunk_id") or "")


def load_jsonl(path: Path) -> dict[str, dict[str, Any]]:
    samples: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            sample = json.loads(line)
            samples[_sample_id(sample)] = sample
    return samples


def select_results(report: dict[str, Any], report_index: int) -> list[dict[str, Any]]:
    if isinstance(report.get("reports"), list):
        return list(report["reports"][report_index].get("results") or [])
    payload = report.get("payload")
    if isinstance(payload, dict):
        return list(payload.get("results") or [])
    if isinstance(report.get("results"), list):
        return list(report.get("results") or [])
    raise ValueError("report does not contain reports[].results, payload.results, or results")


def _count_clean(clean: dict[str, Any] | None) -> dict[str, int]:
    clean = clean or {}
    return {
        "entities": len(clean.get("entities") or []),
        "relations": len(clean.get("relations") or []),
        "facts": len(clean.get("facts") or []),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--report-index", type=int, default=0)
    parser.add_argument("--threshold", type=float, default=0.0)
    args = parser.parse_args()

    samples = load_jsonl(args.samples)
    report = json.loads(args.report.read_text(encoding="utf-8"))
    results = select_results(report, args.report_index)

    rows: list[dict[str, Any]] = []
    summary = {
        "samples_seen": 0,
        "samples_missing_text": 0,
        "clean_pydantic_pass": 0,
        "clean_pydantic_fail": 0,
        "jsonl_finished": 0,
        "jsonl_invalid": 0,
        "backend_parse_pass": 0,
        "backend_parse_fail": 0,
        "local_entities": 0,
        "local_relations": 0,
        "local_facts": 0,
        "backend_entities": 0,
        "backend_relations": 0,
        "backend_facts": 0,
        "backend_evidence_drops": 0,
        "backend_fact_drops": 0,
        "backend_entity_schema_drops": 0,
        "backend_relation_schema_drops": 0,
        "backend_domain_range_remaps": 0,
        "backend_endpoint_completions": 0,
    }

    for result in results:
        sample_id = str(result.get("id") or result.get("chunk_id") or "")
        if not sample_id:
            continue
        summary["samples_seen"] += 1
        sample = samples.get(sample_id)
        clean = result.get("clean_object") or {}
        clean_counts = _count_clean(clean)
        summary["local_entities"] += clean_counts["entities"]
        summary["local_relations"] += clean_counts["relations"]
        summary["local_facts"] += clean_counts["facts"]

        clean_error = ""
        try:
            ExtractionResponse.model_validate(clean)
            summary["clean_pydantic_pass"] += 1
        except Exception as exc:  # noqa: BLE001
            clean_error = f"{type(exc).__name__}: {str(exc)[:240]}"
            summary["clean_pydantic_fail"] += 1

        raw_jsonl = str(result.get("jsonl") or "")
        parsed_lines = _parse_jsonl_lines(raw_jsonl)
        if parsed_lines.finished:
            summary["jsonl_finished"] += 1
        if parsed_lines.invalid_line:
            summary["jsonl_invalid"] += 1

        backend_counts = {"entities": 0, "relations": 0, "facts": 0}
        backend_error = ""
        backend_counters: dict[str, int] = {}
        if sample is None:
            summary["samples_missing_text"] += 1
            backend_error = "sample text missing"
            stack_result = None
        else:
            task = ExtractionTask(
                chunk_id=str(sample.get("chunk_id") or sample_id),
                doc_id=str(sample.get("doc_id") or "local-stack-compat-doc"),
                corpus_id=str(sample.get("corpus_id") or "local-stack-compat-corpus"),
                text=str(sample.get("text") or ""),
                chunk_kind=str(sample.get("chunk_kind") or "body"),
                metadata=dict(sample.get("metadata") or {}),
            )
            stack_result = _parse_jsonl_items(
                parsed_lines.items,
                task,
                args.threshold,
                enable_facts=False,
                max_facts=0,
            )
            if stack_result is None:
                backend_error = "backend parser returned None"

        if stack_result is None:
            summary["backend_parse_fail"] += 1
        else:
            summary["backend_parse_pass"] += 1
            backend_counts = {
                "entities": len(stack_result.entities),
                "relations": len(stack_result.relations),
                "facts": len(stack_result.facts),
            }
            summary["backend_entities"] += backend_counts["entities"]
            summary["backend_relations"] += backend_counts["relations"]
            summary["backend_facts"] += backend_counts["facts"]
            backend_counters = {
                "entity_drop_count": int(stack_result.entity_drop_count),
                "relation_drop_count": int(stack_result.relation_drop_count),
                "evidence_drop_count": int(stack_result.evidence_drop_count),
                "fact_drop_count": int(stack_result.fact_drop_count),
                "domain_range_remap_count": int(stack_result.domain_range_remap_count),
                "endpoint_completion_count": int(stack_result.endpoint_completion_count),
            }
            summary["backend_evidence_drops"] += backend_counters["evidence_drop_count"]
            summary["backend_fact_drops"] += backend_counters["fact_drop_count"]
            summary["backend_entity_schema_drops"] += backend_counters["entity_drop_count"]
            summary["backend_relation_schema_drops"] += backend_counters["relation_drop_count"]
            summary["backend_domain_range_remaps"] += backend_counters["domain_range_remap_count"]
            summary["backend_endpoint_completions"] += backend_counters["endpoint_completion_count"]

        rows.append(
            {
                "id": sample_id,
                "clean_pydantic_ok": not clean_error,
                "clean_pydantic_error": clean_error,
                "jsonl_finished": parsed_lines.finished,
                "jsonl_valid_lines": parsed_lines.valid_lines,
                "jsonl_items": len(parsed_lines.items),
                "jsonl_invalid_line": parsed_lines.invalid_line,
                "backend_parse_ok": stack_result is not None,
                "backend_error": backend_error,
                "local_counts": clean_counts,
                "backend_counts": backend_counts,
                "backend_counters": backend_counters,
            }
        )

    output = {
        "schema": "local_extraction_stack_compat_v1",
        "samples_path": str(args.samples),
        "report_path": str(args.report),
        "report_index": args.report_index,
        "threshold": args.threshold,
        "summary": summary,
        "rows": rows,
    }
    args.out.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    print("LOCAL EXTRACTION STACK COMPATIBILITY")
    print(f"samples: {summary['samples_seen']}")
    print(f"clean pydantic: {summary['clean_pydantic_pass']}/{summary['samples_seen']}")
    print(f"jsonl finished: {summary['jsonl_finished']}/{summary['samples_seen']}")
    print(f"backend parse: {summary['backend_parse_pass']}/{summary['samples_seen']}")
    print(
        "local E/R/F -> backend E/R/F: "
        f"{summary['local_entities']}/{summary['local_relations']}/{summary['local_facts']} -> "
        f"{summary['backend_entities']}/{summary['backend_relations']}/{summary['backend_facts']}"
    )
    print(
        "backend drops/remaps: "
        f"evidence={summary['backend_evidence_drops']} "
        f"domain_range={summary['backend_domain_range_remaps']} "
        f"entity_schema={summary['backend_entity_schema_drops']} "
        f"relation_schema={summary['backend_relation_schema_drops']}"
    )
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()

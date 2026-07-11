#!/usr/bin/env python3
"""Score live three-route reports against reviewed document-level qrels."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


ROUTE_BUDGETS = {
    "Fast Search": 2.0,
    "Hybrid Search": 8.0,
    "Graph Augmentation": 10.0,
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[rank]


def score(report: dict[str, Any], qrels: dict[str, Any]) -> dict[str, Any]:
    judgments = qrels.get("queries") or {}
    evaluated: list[dict[str, Any]] = []
    expected_empty: list[dict[str, Any]] = []
    latency: dict[str, list[float]] = defaultdict(list)
    total_sources = relevant_sources = duplicate_sources = 0
    coverage_values: list[float] = []

    for result in report.get("results") or []:
        route = str(result.get("route") or "")
        query_id = str(result.get("query_id") or "")
        latency[route].append(float((result.get("timings_s") or {}).get("total") or 0.0))
        judgment = judgments.get(query_id)
        if not judgment:
            continue
        sources = result.get("sources") or []
        if route in set(judgment.get("expected_empty_routes") or []):
            expected_empty.append(
                {
                    "query_id": query_id,
                    "route": route,
                    "source_count": len(sources),
                    "passed": len(sources) == 0,
                }
            )
            continue
        if route not in set(judgment.get("evaluated_routes") or []):
            continue

        relevant_ids = set(judgment.get("relevant_doc_ids") or [])
        doc_ids = [str(source.get("doc_id") or "") for source in sources]
        relevant = sum(doc_id in relevant_ids for doc_id in doc_ids)
        duplicates = len(doc_ids) - len(set(doc_ids))
        precision = relevant / len(doc_ids) if doc_ids else 0.0
        planner_coverage = (
            ((result.get("trace_summary") or {}).get("retrieval_diagnostics") or {})
            .get("required_concept_coverage", {})
            .get("coverage")
        )
        coverage = float(
            planner_coverage
            if planner_coverage is not None
            else (
                (result.get("source_anchor_coverage") or {}).get(
                    "required_coverage"
                )
                or 0.0
            )
        )
        total_sources += len(doc_ids)
        relevant_sources += relevant
        duplicate_sources += duplicates
        if not judgment.get("coverage_exempt"):
            coverage_values.append(coverage)
        evaluated.append(
            {
                "query_id": query_id,
                "route": route,
                "source_count": len(doc_ids),
                "relevant_sources": relevant,
                "citation_precision": round(precision, 4),
                "duplicate_sources": duplicates,
                "required_concept_coverage": round(coverage, 4),
                "irrelevant_doc_ids": [
                    doc_id for doc_id in doc_ids if doc_id not in relevant_ids
                ],
            }
        )

    citation_precision = relevant_sources / total_sources if total_sources else 0.0
    duplicate_rate = duplicate_sources / total_sources if total_sources else 0.0
    concept_coverage = (
        sum(coverage_values) / len(coverage_values) if coverage_values else 0.0
    )
    route_latency = {
        route: {
            "samples": len(values),
            "p95_s": round(_percentile(values, 0.95), 4),
            "budget_s": ROUTE_BUDGETS.get(route),
            "passed": _percentile(values, 0.95) <= ROUTE_BUDGETS.get(route, float("inf")),
        }
        for route, values in sorted(latency.items())
    }
    gates = {
        "citation_precision_at_least_95pct": citation_precision >= 0.95,
        "duplicate_evidence_below_5pct": duplicate_rate < 0.05,
        "required_concept_coverage_at_least_90pct": concept_coverage >= 0.90,
        "route_latency_p95": all(item["passed"] for item in route_latency.values()),
        "expected_empty_routes": all(item["passed"] for item in expected_empty),
    }
    return {
        "status": "pass" if all(gates.values()) else "fail",
        "gates": gates,
        "summary": {
            "evaluated_cases": len(evaluated),
            "total_sources": total_sources,
            "relevant_sources": relevant_sources,
            "citation_precision": round(citation_precision, 4),
            "duplicate_sources": duplicate_sources,
            "duplicate_evidence_rate": round(duplicate_rate, 4),
            "mean_required_concept_coverage": round(concept_coverage, 4),
        },
        "route_latency": route_latency,
        "expected_empty": expected_empty,
        "cases": evaluated,
        "qrels_version": qrels.get("version"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument(
        "--qrels",
        type=Path,
        default=Path(__file__).with_name("retrieval_ecommerce_mark_qrels.json"),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--assert", dest="assert_mode", action="store_true")
    args = parser.parse_args()
    result = score(_load(args.report), _load(args.qrels))
    payload = json.dumps(result, indent=2, sort_keys=True)
    print(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 1 if args.assert_mode and result["status"] != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main())

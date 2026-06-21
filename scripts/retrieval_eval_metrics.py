#!/usr/bin/env python3
"""Offline retrieval metric reporter.

Input JSON/JSONL rows should already contain route outputs and relevance
labels; this script does not call the live backend.

Example row:
{
  "query": "what is python and is ai essentially python",
  "route": "Graph Augmentation",
  "ranked_chunk_ids": ["chunk_a", "chunk_b", "chunk_c"],
  "relevance": {"chunk_a": 3, "chunk_b": 1},
  "exact_source_ids": ["chunk_a"],
  "latency_ms": 7200,
  "answer_sufficient": true,
  "atom_coverage": 0.9,
  "facts_used": 12,
  "relations_used": 19,
  "graph_advantage_score": 0.86,
  "doc_ids": ["doc_1", "doc_2", "doc_3"]
}
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
METRICS_PATH = ROOT / "backend" / "services" / "retriever" / "eval_metrics.py"
_spec = importlib.util.spec_from_file_location("retrieval_eval_metrics_core", METRICS_PATH)
if _spec is None or _spec.loader is None:
    raise SystemExit(f"Could not load metrics module at {METRICS_PATH}")
_metrics = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _metrics
_spec.loader.exec_module(_metrics)

case_from_mapping = _metrics.case_from_mapping
route_metric_profile = _metrics.route_metric_profile
summarize_route_eval = _metrics.summarize_route_eval


def _load_rows(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        return [
            json.loads(line)
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
    data = json.loads(text)
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("cases"), list):
        return data["cases"]
    raise SystemExit("Input must be a JSON array, a JSON object with cases[], or JSONL rows.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute offline retrieval MRR/MAP/NDCG and latency summaries."
    )
    parser.add_argument("input", type=Path, help="Golden-set JSON or JSONL file")
    parser.add_argument("--mrr-k", type=int, default=5)
    parser.add_argument("--recall-k", type=int, default=20)
    parser.add_argument("--map-k", type=int, default=20)
    parser.add_argument("--ndcg-k", type=int, default=8)
    parser.add_argument("--source-k", type=int, default=8)
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    args = parser.parse_args()

    rows = _load_rows(args.input)
    cases = [case_from_mapping(row) for row in rows]
    grouped: dict[str, list] = defaultdict(list)
    for case in cases:
        grouped[case.route or "unknown"].append(case)

    output = {
        "metric_contract": {
            "MRR@5": "first good hit; most important for Fast Search",
            "Recall@20": "candidate-pool recall; did enough known-relevant chunks surface?",
            "MAP@20": "offline candidate-recall diagnostic; not the Graph headline metric",
            "NDCG@8": "graded final evidence-pack quality; most important for Graph Augmentation",
            "ExactSourceRecall@8": "exact-span/source-recovery slice; did the route recover the gold passage ids?",
            "answer_sufficiency": "selected evidence can answer the question",
            "route_rule": {
                "Fast Search": ["MRR@5", "Recall@20", "latency_p95_ms"],
                "Hybrid Search": [
                    "MRR@5",
                    "Recall@20",
                    "MAP@20",
                    "NDCG@8",
                    "ExactSourceRecall@8",
                    "unique_doc_count",
                    "near_duplicate_rate",
                ],
                "Graph Augmentation": [
                    "NDCG@8",
                    "answer_sufficiency_rate",
                    "ExactSourceRecall@8",
                    "graph_advantage",
                    "atom_coverage",
                    "facts_used",
                    "relations_used",
                    "multi_doc_evidence_rate",
                    "near_duplicate_rate",
                    "latency_p95_ms",
                ],
            },
            "note": "Offline eval only; these metrics are not computed in the live query path.",
        },
        "routes": {},
    }
    for route, route_cases in sorted(grouped.items()):
        output["routes"][route] = {
            "profile": route_metric_profile(route),
            **summarize_route_eval(
                route_cases,
                mrr_k=args.mrr_k,
                recall_k=args.recall_k,
                map_k=args.map_k,
                ndcg_k=args.ndcg_k,
                source_k=args.source_k,
            ),
        }

    print(json.dumps(output, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

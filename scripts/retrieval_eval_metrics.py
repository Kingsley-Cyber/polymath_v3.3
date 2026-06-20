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
  "latency_ms": 7200,
  "answer_sufficient": true
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
    parser.add_argument("--map-k", type=int, default=20)
    parser.add_argument("--ndcg-k", type=int, default=8)
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
            "MRR": "first relevant result appears early",
            "MAP": "many relevant chunks retrieved early",
            "NDCG": "graded final evidence-pack ranking quality",
            "answer_sufficiency": "selected evidence can answer the question",
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
                map_k=args.map_k,
                ndcg_k=args.ndcg_k,
            ),
        }

    print(json.dumps(output, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

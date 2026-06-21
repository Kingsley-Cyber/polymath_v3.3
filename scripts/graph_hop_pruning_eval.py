#!/usr/bin/env python3
"""Live graph hop/pruning validation.

This is not an LLM-answer eval. It exercises `/api/graph/query` directly so
you can inspect what graph context would be injected before generation.

Examples:
  python3 scripts/graph_hop_pruning_eval.py \
    --corpus-id f8a0aa85-6cb4-4f64-a973-f9183f1546bb \
    "what is python and is ai essentially python" --pretty --assert

  python3 scripts/graph_hop_pruning_eval.py \
    --corpus-id f8a0aa85-6cb4-4f64-a973-f9183f1546bb \
    "why are ontologies powerful" --hops 1 2 --limit 50 --pretty
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from services.graph.hop_pruning_validation import (  # noqa: E402
    compare_hop_summaries,
    summarize_graph_query_payload,
    validate_graph_hop_report,
)


def _display_summary(summary: dict[str, Any], *, include_ids: bool) -> dict[str, Any]:
    if include_ids:
        return summary
    public = dict(summary)
    node_ids = list(public.pop("node_ids", []) or [])
    edge_keys = list(public.pop("edge_keys", []) or [])
    public["sample_node_ids"] = node_ids[:12]
    public["sample_edge_keys"] = edge_keys[:12]
    public["hidden_node_ids"] = max(0, len(node_ids) - 12)
    public["hidden_edge_keys"] = max(0, len(edge_keys) - 12)
    return public


def _post_graph_query(
    *,
    base_url: str,
    corpus_ids: list[str],
    query: str,
    max_hops: int,
    limit: int,
    seed_limit_per_token: int,
    timeout_s: float,
) -> tuple[dict[str, Any], float]:
    body = {
        "corpus_ids": corpus_ids,
        "query": query,
        "max_hops": max_hops,
        "limit": limit,
        "seed_limit_per_token": seed_limit_per_token,
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/graph/query",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    start = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload, time.perf_counter() - start


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Compare graph hop counts and edge-pruning quality using the live "
            "/api/graph/query endpoint."
        )
    )
    parser.add_argument(
        "query",
        nargs="?",
        default="what is python and is ai essentially python",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("GRAPH_EVAL_BASE", "http://localhost:8000"),
    )
    parser.add_argument(
        "--corpus-id",
        action="append",
        default=[],
        help="Corpus id. Repeat for multi-corpus. Defaults to GRAPH_EVAL_CORPUS or the local smoke corpus.",
    )
    parser.add_argument("--hops", nargs="+", type=int, default=[1, 2])
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--seed-limit-per-token", type=int, default=3)
    parser.add_argument("--timeout-s", type=float, default=35.0)
    parser.add_argument("--max-hop2-links", type=int, default=360)
    parser.add_argument("--max-context-bloat-delta", type=float, default=180.0)
    parser.add_argument("--max-generic-ratio", type=float, default=0.45)
    parser.add_argument("--max-weak-or-thin-ratio", type=float, default=0.30)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument(
        "--include-ids",
        action="store_true",
        help="Include full node_ids and edge_keys arrays in the report.",
    )
    parser.add_argument(
        "--assert",
        dest="assert_mode",
        action="store_true",
        help="Exit non-zero when validation issues are found.",
    )
    args = parser.parse_args()

    corpus_ids = args.corpus_id or [
        os.environ.get("GRAPH_EVAL_CORPUS", "f8a0aa85-6cb4-4f64-a973-f9183f1546bb")
    ]
    hops = sorted({max(1, int(hop)) for hop in args.hops})
    if 1 not in hops:
        hops.insert(0, 1)
    if 2 not in hops:
        hops.append(2)

    runs: dict[str, dict[str, Any]] = {}
    for hop in hops:
        payload, latency_s = _post_graph_query(
            base_url=args.base_url,
            corpus_ids=corpus_ids,
            query=args.query,
            max_hops=hop,
            limit=args.limit,
            seed_limit_per_token=args.seed_limit_per_token,
            timeout_s=args.timeout_s,
        )
        runs[str(hop)] = summarize_graph_query_payload(payload, latency_s=latency_s)

    comparison = compare_hop_summaries(runs["1"], runs["2"])
    issues = validate_graph_hop_report(
        hop1=runs["1"],
        hop2=runs["2"],
        comparison=comparison,
        max_latency_s=args.timeout_s,
        max_hop2_links=args.max_hop2_links,
        max_context_bloat_delta=args.max_context_bloat_delta,
        max_generic_ratio=args.max_generic_ratio,
        max_weak_or_thin_ratio=args.max_weak_or_thin_ratio,
    )
    report = {
        "query": args.query,
        "corpus_ids": corpus_ids,
        "route": "/api/graph/query",
        "validation_contract": {
            "default_hops": "1-2",
            "edge_property_pruning": (
                "generic/weak/thin/no-evidence edges should not dominate "
                "the returned graph context"
            ),
            "hop_count": (
                "hop 2 should add measurable coverage without excessive "
                "context-bloat or drift"
            ),
            "note": (
                "This validates retrieved graph context before generation; "
                "MRR/MAP/NDCG still require a labeled golden set."
            ),
        },
        "runs": {
            hop: _display_summary(summary, include_ids=args.include_ids)
            for hop, summary in runs.items()
        },
        "comparison": comparison,
        "issues": issues,
        "status": "pass" if not issues else "fail",
    }
    print(json.dumps(report, indent=2 if args.pretty else None, sort_keys=True))
    return 1 if args.assert_mode and issues else 0


if __name__ == "__main__":
    raise SystemExit(main())

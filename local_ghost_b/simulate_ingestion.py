"""
End-to-end broke-mode ingestion simulation.

Runs the cascade over real chunks and reports operational metrics so you can see
how the local graph behaves before wiring it into the backend:

    chunks/sec, candidate-pairs/sec
    exact edges, related_to edges, dropped
    exact edges per chunk, related_to per chunk
    predicate distribution (what the local graph is made of)
    evidence pass rate (edges with non-empty evidence)
    [gold mode] agreement with Ghost B's own predicate (exact-tier only)

Two pair modes (see ghost_b_cascade_infer):
    --pair_mode cooccur : derive pairs from entities (TRUE broke-mode, realistic)
    --pair_mode gold    : reuse the chunk's relation pairs (CEILING)

Set HF_HUB_OFFLINE=1.
"""

from __future__ import annotations

import argparse
import collections
import json
import time
from pathlib import Path

import os

from ghost_b_cascade_infer import (apply_related_cap, candidate_pairs,
                                    iter_chunks, pairs_from_gold_relations)
from polymath_local_extractor import LocalExtractor


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks", required=True)
    ap.add_argument("--runs_dir", default="runs")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--pair_mode", choices=["cooccur", "gold"], default="cooccur")
    ap.add_argument("--out", default="data_eval/ingestion_report.json")
    args = ap.parse_args()

    ex = LocalExtractor(args.runs_dir)
    max_related = int(os.environ.get("LOCAL_GHOST_B_MAX_RELATED_TO_PER_CHUNK") or 3)
    print(f"[config] {json.dumps(ex.config_summary())}", flush=True)
    print(f"[mode] pair_mode={args.pair_mode}  max_related_to/chunk={max_related}", flush=True)

    tiers = collections.Counter()
    pred_dist = collections.Counter()
    sources = collections.Counter()
    n_chunks = n_pairs = n_with_ev = 0
    gold_exact_total = gold_exact_correct = 0

    t0 = time.time()
    for chunk in iter_chunks(args.chunks, args.limit or None):
        n_chunks += 1
        pairs = (pairs_from_gold_relations(chunk) if args.pair_mode == "gold"
                 else candidate_pairs(chunk))
        if not pairs:
            continue
        edges = apply_related_cap(ex.extract(pairs), max_related)
        n_pairs += len(pairs)
        for e, p in zip(edges, pairs):
            tiers[e.tier] += 1
            sources[e.source] += 1
            if e.tier != "drop":
                pred_dist[e.predicate] += 1
                if p.get("text"):
                    n_with_ev += 1
            if args.pair_mode == "gold" and e.tier in ("tier1_exact", "tier2_family"):
                gold = p.get("_gold")
                if gold is not None:
                    gold_exact_total += 1
                    if e.predicate == gold:
                        gold_exact_correct += 1
    dt = time.time() - t0

    n_exact = tiers.get("tier1_exact", 0) + tiers.get("tier2_family", 0)
    n_related = tiers.get("tier3_related", 0)
    n_drop = tiers.get("drop", 0)
    written = n_exact + n_related

    print(f"\n=== THROUGHPUT ===")
    print(f"  wall time        : {dt:.1f}s")
    print(f"  chunks           : {n_chunks}  ({n_chunks/dt:.1f}/s)")
    print(f"  candidate pairs  : {n_pairs}  ({n_pairs/dt:.0f}/s)")

    print(f"\n=== EDGE OUTCOMES ===")
    print(f"  exact edges      : {n_exact}  ({100*n_exact/max(n_pairs,1):.1f}% of pairs)")
    print(f"    tier1_exact    : {tiers.get('tier1_exact',0)}")
    print(f"    tier2_family   : {tiers.get('tier2_family',0)}")
    print(f"  related_to       : {n_related}  ({100*n_related/max(n_pairs,1):.1f}%)")
    print(f"  dropped          : {n_drop}  ({100*n_drop/max(n_pairs,1):.1f}%)")
    print(f"  exact/chunk      : {n_exact/max(n_chunks,1):.2f}")
    print(f"  related/chunk    : {n_related/max(n_chunks,1):.2f}")
    print(f"  evidence present : {100*n_with_ev/max(written,1):.1f}% of written edges")

    if args.pair_mode == "gold" and gold_exact_total:
        print(f"\n=== AGREEMENT WITH GHOST B (exact-tier, gold mode) ===")
        print(f"  exact predicate agreement: {gold_exact_correct}/{gold_exact_total} "
              f"= {gold_exact_correct/gold_exact_total:.3f}")

    print(f"\n=== PREDICATE DISTRIBUTION (written graph) ===")
    for p, c in pred_dist.most_common():
        print(f"  {p:<16} {c:>6}  ({100*c/max(written,1):.1f}%)")

    report = {
        "pair_mode": args.pair_mode,
        "wall_s": dt, "chunks": n_chunks, "pairs": n_pairs,
        "chunks_per_s": n_chunks/dt if dt else 0,
        "pairs_per_s": n_pairs/dt if dt else 0,
        "tiers": dict(tiers),
        "exact_edges": n_exact, "related_to": n_related, "dropped": n_drop,
        "exact_per_chunk": n_exact/max(n_chunks,1),
        "evidence_pass_rate": n_with_ev/max(written,1),
        "predicate_distribution": dict(pred_dist),
        "config": ex.config_summary(),
    }
    if args.pair_mode == "gold" and gold_exact_total:
        report["ghost_b_agreement"] = gold_exact_correct/gold_exact_total
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n[done] {args.out}")


if __name__ == "__main__":
    main()

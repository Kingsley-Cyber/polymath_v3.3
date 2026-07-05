"""
End-to-end evaluation of the broke-mode local extractor cascade.

Runs LocalExtractor over a held-out eval set (with gold predicates) and reports:
  - exact-edge precision  : of edges written as an EXACT predicate (tier1/tier2),
                            fraction whose predicate == gold
  - exact-edge coverage   : fraction of all pairs that got an exact predicate
  - related_to fallback   : fraction routed to related_to (tier3)
  - drop rate             : fraction dropped
  - per-predicate exact precision/coverage (vs gold)
  - "useful" precision    : counts related_to as correct only when gold == related_to
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

from polymath_local_extractor import LocalExtractor


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs_dir", default="runs")
    ap.add_argument("--eval_file", default="data_eval/eval.jsonl")
    ap.add_argument("--out", default="data_eval/cascade_report.json")
    args = ap.parse_args()

    rows = [json.loads(l) for l in Path(args.eval_file).read_text(encoding="utf-8").splitlines() if l.strip()]
    gold = [r["gold_predicate"] for r in rows]
    print(f"[eval] {len(rows)} pairs", flush=True)

    ex = LocalExtractor(args.runs_dir)
    edges = ex.extract(rows)

    tier_counts = collections.Counter(e.tier for e in edges)
    # exact = tier1_exact or tier2_family (we committed to a specific predicate)
    exact_mask = [e.tier in ("tier1_exact", "tier2_family") for e in edges]
    n = len(edges)
    n_exact = sum(exact_mask)
    exact_correct = sum(1 for e, g, m in zip(edges, gold, exact_mask) if m and e.predicate == g)

    # related_to tier
    n_related = tier_counts.get("tier3_related", 0)
    n_drop = tier_counts.get("drop", 0)

    exact_precision = exact_correct / n_exact if n_exact else 0.0
    exact_coverage = n_exact / n if n else 0.0

    # "useful precision": an edge is useful-correct if exact&right, OR related_to and gold is related_to
    useful_correct = exact_correct + sum(
        1 for e, g in zip(edges, gold)
        if e.tier == "tier3_related" and e.predicate == "related_to" and g == "related_to"
    )

    # per-predicate (gold-conditioned) exact precision/coverage
    per_pred = collections.defaultdict(lambda: {"gold_n": 0, "exact_fired": 0, "exact_correct": 0})
    for e, g, m in zip(edges, gold, exact_mask):
        per_pred[g]["gold_n"] += 1
        if m:
            per_pred[g]["exact_fired"] += 1
            if e.predicate == g:
                per_pred[g]["exact_correct"] += 1

    # predicted-predicate precision (of all exact edges labeled X, how many gold==X)
    pred_precision = collections.defaultdict(lambda: {"fired": 0, "correct": 0})
    for e, g, m in zip(edges, gold, exact_mask):
        if m:
            pred_precision[e.predicate]["fired"] += 1
            if e.predicate == g:
                pred_precision[e.predicate]["correct"] += 1

    print("\n=== TIERS ===")
    for t in ("tier1_exact", "tier2_family", "tier3_related", "drop"):
        print(f"  {t:<14} {tier_counts.get(t,0):>5}  ({100*tier_counts.get(t,0)/n:.1f}%)")

    print("\n=== HEADLINE ===")
    print(f"  exact-edge precision : {exact_precision:.3f}  (correct {exact_correct}/{n_exact})")
    print(f"  exact-edge coverage  : {exact_coverage:.3f}  ({n_exact}/{n})")
    print(f"  related_to fallback  : {n_related/n:.3f}")
    print(f"  drop rate            : {n_drop/n:.3f}")

    print("\n=== PER-GOLD-PREDICATE (exact precision / coverage vs gold) ===")
    for g in sorted(per_pred, key=lambda k: -per_pred[k]["gold_n"]):
        d = per_pred[g]
        prec = d["exact_correct"] / d["exact_fired"] if d["exact_fired"] else 0.0
        cov = d["exact_correct"] / d["gold_n"] if d["gold_n"] else 0.0
        print(f"  {g:<16} P={prec:.2f} cov={cov:.2f}  (gold_n={d['gold_n']}, fired={d['exact_fired']})")

    print("\n=== PREDICTED-PREDICATE precision (of edges written as X) ===")
    for p in sorted(pred_precision, key=lambda k: -pred_precision[k]["fired"]):
        d = pred_precision[p]
        prec = d["correct"] / d["fired"] if d["fired"] else 0.0
        print(f"  {p:<16} P={prec:.2f}  (fired {d['fired']})")

    report = {
        "n": n,
        "tiers": dict(tier_counts),
        "exact_precision": exact_precision,
        "exact_coverage": exact_coverage,
        "related_fallback_rate": n_related / n if n else 0.0,
        "drop_rate": n_drop / n if n else 0.0,
        "useful_correct": useful_correct,
        "per_gold_predicate": {k: dict(v) for k, v in per_pred.items()},
        "predicted_precision": {k: dict(v) for k, v in pred_precision.items()},
    }
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\n[done] {args.out}")


if __name__ == "__main__":
    main()

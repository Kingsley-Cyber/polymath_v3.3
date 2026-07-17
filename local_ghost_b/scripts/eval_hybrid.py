"""
Compare BERT-only cascade vs BERT+Qwen hybrid on the clean held-out eval set.
Reports exact-edge precision/coverage and how many ambiguous edges Qwen recovered.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path
from collections import Counter

from qwen_resolver import HybridExtractor
from polymath_local_extractor import LocalExtractor


def score(edges, gold, label):
    exact = [(e.tier in ("tier1_exact", "tier2_family", "qwen_resolved")) for e in edges]
    n = len(edges)
    n_exact = sum(exact)
    correct = sum(1 for e, g, m in zip(edges, gold, exact) if m and e.predicate == g)
    prec = correct / n_exact if n_exact else 0.0
    cov = n_exact / n if n else 0.0
    print(f"\n=== {label} ===")
    print(f"  exact precision : {prec:.3f}  ({correct}/{n_exact})")
    print(f"  exact coverage  : {cov:.3f}  ({n_exact}/{n})")
    return {"precision": prec, "coverage": cov, "n_exact": n_exact, "correct": correct, "n": n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval_file", default="data_eval/eval.jsonl")
    ap.add_argument("--runs_dir", default="runs")
    ap.add_argument("--qwen_dir", default="runs/qwen_resolver_v1/merged")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="data_eval/hybrid_report.json")
    args = ap.parse_args()

    rows = [json.loads(l) for l in Path(args.eval_file).read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.limit:
        rows = rows[:args.limit]
    gold = [r["gold_predicate"] for r in rows]
    print(f"[eval] {len(rows)} held-out pairs", flush=True)

    # BERT-only baseline
    bert = LocalExtractor(args.runs_dir)
    bert_edges = bert.extract(rows)
    bert_score = score(bert_edges, gold, "BERT-only cascade")
    del bert

    # Hybrid
    hyb = HybridExtractor(args.runs_dir, args.qwen_dir)
    hyb_edges = hyb.extract(rows)
    hyb_score = score(hyb_edges, gold, "BERT + Qwen hybrid")

    # what Qwen did
    qwen_edges = [(e, g) for e, g in zip(hyb_edges, gold) if e.tier == "qwen_resolved"]
    qwen_correct = sum(1 for e, g in qwen_edges if e.predicate == g)
    print(f"\n=== QWEN CONTRIBUTION ===")
    print(f"  ambiguous edges Qwen committed : {len(qwen_edges)}")
    if qwen_edges:
        print(f"  of those, correct vs gold      : {qwen_correct}/{len(qwen_edges)} = {qwen_correct/len(qwen_edges):.3f}")
    print(f"  coverage gain  : {bert_score['coverage']:.3f} -> {hyb_score['coverage']:.3f}")
    print(f"  precision      : {bert_score['precision']:.3f} -> {hyb_score['precision']:.3f}")

    Path(args.out).write_text(json.dumps({
        "bert_only": bert_score, "hybrid": hyb_score,
        "qwen_committed": len(qwen_edges), "qwen_correct": qwen_correct,
    }, indent=2), encoding="utf-8")
    print(f"\n[done] {args.out}")


if __name__ == "__main__":
    main()

"""
The fast-lane metric that actually matters: per-predicate PRECISION and
COVERAGE at a softmax-confidence threshold.

A routing layer is good if, when it fires a predicate above threshold, it is
almost always right (high precision) on a useful fraction of pairs (coverage).
Macro F1 including 'none' is the wrong gate for this.

For each threshold t, for each non-none predicate:
    fired       = #(argmax==pred AND maxprob>=t)
    correct     = #(argmax==pred AND maxprob>=t AND true==pred)
    precision   = correct / fired
    coverage    = fired / #(true==pred)        (recall of confident firings)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--checkpoint", required=True)
    p.add_argument("--val_file", required=True)
    p.add_argument("--label_map_file", required=True)
    p.add_argument("--none_label", default="none")
    p.add_argument("--max_length", type=int, default=192)
    p.add_argument("--batch_size", type=int, default=256)
    p.add_argument("--thresholds", default="0.0,0.5,0.7,0.8,0.9,0.95")
    return p.parse_args()


def build_input(r, sep):
    base = (f"{r['text']} {sep} {r['subject']} {sep} {r['subject_type']} {sep} "
            f"{r['object']} {sep} {r['object_type']}")
    if r.get("cue"):
        base += f" {sep} {r['cue']}"
    return base


def main():
    args = parse_args()
    lm = json.loads(Path(args.label_map_file).read_text(encoding="utf-8"))
    labels = [l for l, _ in sorted(lm["label2id"].items(), key=lambda kv: kv[1])]
    label2id = {l: i for i, l in enumerate(labels)}

    tok = AutoTokenizer.from_pretrained(args.checkpoint)
    sep = tok.sep_token or "[SEP]"
    model = AutoModelForSequenceClassification.from_pretrained(
        args.checkpoint, dtype=torch.bfloat16).to("cuda").eval()

    rows = [json.loads(l) for l in Path(args.val_file).read_text(encoding="utf-8").splitlines() if l.strip()]
    inputs = [build_input(r, sep) for r in rows]
    truth = np.array([label2id[r["label"]] for r in rows])

    probs = np.zeros((len(rows), len(labels)), dtype=np.float32)
    with torch.inference_mode():
        for s in range(0, len(rows), args.batch_size):
            enc = tok(inputs[s:s+args.batch_size], padding=True, truncation=True,
                      max_length=args.max_length, return_tensors="pt").to("cuda")
            logits = model(**enc).logits.float()
            probs[s:s+enc["input_ids"].shape[0]] = torch.softmax(logits, dim=-1).cpu().numpy()

    pred = probs.argmax(axis=1)
    maxp = probs.max(axis=1)
    none_id = label2id.get(args.none_label, -1)
    thresholds = [float(x) for x in args.thresholds.split(",")]

    print(f"{'pred':<14}{'true_n':>7}", end="")
    for t in thresholds:
        print(f"  | t>={t:<4} P/cov", end="")
    print()
    print("-" * (21 + len(thresholds) * 18))

    summary = {}
    for lab in labels:
        if lab == args.none_label:
            continue
        lid = label2id[lab]
        true_n = int((truth == lid).sum())
        line = f"{lab:<14}{true_n:>7}"
        summary[lab] = {}
        for t in thresholds:
            fired_mask = (pred == lid) & (maxp >= t)
            fired = int(fired_mask.sum())
            correct = int((fired_mask & (truth == lid)).sum())
            prec = correct / fired if fired else 0.0
            cov = correct / true_n if true_n else 0.0
            line += f"  | {prec:.2f}/{cov:.2f}"
            summary[lab][t] = {"precision": prec, "coverage": cov, "fired": fired}
        print(line)

    # Aggregate: at each threshold, how many pairs get a trusted exact edge,
    # and what's the blended precision across all non-none predicates.
    print("\n=== aggregate over all easy predicates ===")
    for t in thresholds:
        non_none = np.array([i for i, l in enumerate(labels) if l != args.none_label])
        fired_mask = np.isin(pred, non_none) & (maxp >= t)
        fired = int(fired_mask.sum())
        correct = int((fired_mask & (pred == truth)).sum())
        prec = correct / fired if fired else 0.0
        total_non_none = int(np.isin(truth, non_none).sum())
        cov = correct / total_non_none if total_non_none else 0.0
        print(f"  t>={t:<4}  fired={fired:<5} precision={prec:.3f}  coverage={cov:.3f}")

    out = Path(args.checkpoint).parent / "threshold_sweep.json"
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n[done] {out}")


if __name__ == "__main__":
    main()

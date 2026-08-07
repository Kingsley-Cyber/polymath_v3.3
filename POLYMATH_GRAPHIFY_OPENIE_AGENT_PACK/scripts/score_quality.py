#!/usr/bin/env python3
from __future__ import annotations
import json, argparse
from pathlib import Path


def safe_div(a,b): return a/b if b else 0.0

def f1(p,r): return safe_div(2*p*r, p+r)


def main():
    ap = argparse.ArgumentParser(description="Score extraction artifacts against fixture gold sidecar")
    ap.add_argument("--gold", required=True)
    ap.add_argument("--pred", required=True, help="JSON with entities and relations arrays")
    ap.add_argument("--out")
    args = ap.parse_args()
    gold = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    pred = json.loads(Path(args.pred).read_text(encoding="utf-8"))
    g_ents = {(e["start"], e["end"], e["label"]) for e in gold.get("entities", [])}
    p_ents = {(e["start"], e["end"], e.get("label") or e.get("predicted_type")) for e in pred.get("entities", [])}
    tp = len(g_ents & p_ents)
    ep = safe_div(tp, len(p_ents)); er = safe_div(tp, len(g_ents))
    result = {"entity_exact_span_precision": ep, "entity_exact_span_recall": er, "entity_exact_span_f1": f1(ep, er), "gold_entities": len(g_ents), "pred_entities": len(p_ents)}
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.out: Path(args.out).write_text(text, encoding="utf-8")
    print(text)

if __name__ == "__main__": main()

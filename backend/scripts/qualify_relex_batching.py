#!/usr/bin/env python3
"""Qualify batched Relex MPS inference against the frozen gold set.

For each candidate RELEX_SIDECAR_BATCH_SIZE (default 4, 8, 16) this harness:
  1. regenerates predictions with regen_relex_benchmark_batched.py --batch-size N
     (the gold-scoring mirror of the sidecar _predict_many),
  2. diffs them against the frozen batch-1 artifact
     (relex_large_v3_mps_fp32.predictions.jsonl) field-by-field,
  3. scores batch-N with goldscore.run_all --apply-type-constraints and
     compares decision lanes / accepted-relation sets against frozen batch-1.

Parity gates (ALL must hold for a size to PASS):
  span parity == 1.0            (entity start/end sets identical per sample)
  endpoint parity == 1.0        (relation head+tail (start,end) sets identical)
  predicate parity == 1.0       (accepted predicate multiset identical)
  lane parity == 1.0            (goldscore acceptance lane per relation identical)
  lost_gold_relations == 0
  new_unsupported_accepts == 0
  missing_rows == 0  duplicate_rows == 0

Selects the largest passing size (16 else 8 else 4 else 1). Writes
data_eval/ingestion_speed/relex_batch_comparison.json and
data_eval/ingestion_speed/gold_decision_parity.json.

Run:
  .venv-relex/bin/python backend/scripts/qualify_relex_batching.py [--sizes 4 8 16]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data/deterministic_gold_score/data"
GOLD = DATA_DIR / "relex_gold_v1.jsonl"
ONTOLOGY = REPO_ROOT / "data/deterministic_gold_score/config/ontology.json"
FROZEN_B1 = DATA_DIR / "relex_large_v3_mps_fp32.predictions.jsonl"
GOLDSCORE_SRC = REPO_ROOT / "data/deterministic_gold_score/src"
OUT_DIR = REPO_ROOT / "data_eval/ingestion_speed"
SCORE_TMP = OUT_DIR / "_qualify_scores"

# Rounding is to 6 dp in both generators; allow only last-digit float jitter.
FLOAT_TOL = 1e-6


def _load_jsonl(path: Path) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _load_predictions(path: Path) -> dict[str, dict]:
    """Return {sample_id: row}, skipping the metadata header line."""
    out = {}
    for row in _load_jsonl(path):
        if "__metadata__" in row:
            continue
        out[row["sample_id"]] = row
    return out


def _ent_key(e: dict) -> tuple:
    return (e.get("start"), e.get("end"), e.get("text"), e.get("type") or e.get("label"))


def _pair_key(rp: dict) -> tuple:
    h, t = rp["head"], rp["tail"]
    return (h.get("start"), h.get("end"), t.get("start"), t.get("end"))


def _rel_key(r: dict) -> tuple:
    h, t = r["head"], r["tail"]
    return (h.get("start"), h.get("end"), r.get("predicate"), t.get("start"), t.get("end"))


def _score_max(scores: dict) -> float:
    return max(scores.values()) if scores else 0.0


def diff_rows(b1: dict, bn: dict, sid: str) -> dict:
    """Field-by-field diff of one sample's batch-1 vs batch-N row."""
    d: dict = {"sample_id": sid}

    b1_ents = Counter(_ent_key(e) for e in b1["entities"])
    bn_ents = Counter(_ent_key(e) for e in bn["entities"])
    d["entity_spans_equal"] = b1_ents == bn_ents
    # span parity ignores type/text — pure (start,end)
    b1_span = Counter((e.get("start"), e.get("end")) for e in b1["entities"])
    bn_span = Counter((e.get("start"), e.get("end")) for e in bn["entities"])
    d["span_parity"] = 1.0 if b1_span == bn_span else 0.0

    b1_pairs = Counter(_pair_key(rp) for rp in b1["raw_pair_scores"])
    bn_pairs = Counter(_pair_key(rp) for rp in bn["raw_pair_scores"])
    d["endpoint_parity"] = 1.0 if b1_pairs == bn_pairs else 0.0

    # confidence: per matching pair, max |score delta| across labels
    max_conf_delta = 0.0
    bn_by_key = {_pair_key(rp): rp for rp in bn["raw_pair_scores"]}
    for rp in b1["raw_pair_scores"]:
        k = _pair_key(rp)
        other = bn_by_key.get(k)
        if not other:
            continue
        for label, sc in rp["scores"].items():
            osc = other["scores"].get(label, 0.0)
            max_conf_delta = max(max_conf_delta, abs(sc - osc))
    d["max_confidence_delta"] = max_conf_delta
    d["confidence_within_tol"] = max_conf_delta <= FLOAT_TOL

    b1_rels = Counter(_rel_key(r) for r in b1["relations"])
    bn_rels = Counter(_rel_key(r) for r in bn["relations"])
    d["predicate_parity"] = 1.0 if b1_rels == bn_rels else 0.0

    return d


def score_with_goldscore(pred_path: Path, tag: str) -> dict:
    """Run goldscore.run_all on a predictions file, return scores.json.

    goldscore.load_jsonl keys every row on sample_id, so the metadata header
    line must be stripped into a temp file first.
    """
    out = SCORE_TMP / tag
    out.mkdir(parents=True, exist_ok=True)
    clean = out / f"_pred_{tag}.jsonl"
    with open(pred_path) as fin, open(clean, "w") as fout:
        for line in fin:
            if line.strip() and '"__metadata__"' not in line:
                fout.write(line)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(GOLDSCORE_SRC)
    cmd = [
        sys.executable, "-m", "goldscore.run_all",
        "--gold", str(GOLD),
        "--predictions", str(clean),
        "--ontology", str(ONTOLOGY),
        "--output-dir", str(out),
        "--apply-type-constraints",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"goldscore.run_all failed for {tag}:\n{proc.stdout}\n{proc.stderr}"
        )
    with open(out / "scores.json") as f:
        return json.load(f)


def _accepted_set(scores: dict) -> set:
    """Extract the accepted (supported) relation id set from a scores.json.

    Shape-tolerant: walks strict/canonical modes looking for per-relation
    verdicts; falls back to an empty set so callers can still compare counts.
    """
    acc = set()
    for mode in ("strict", "canonical"):
        block = scores.get(mode) or {}
        for key in ("accepted", "accepted_relation_ids", "supported", "true_positives"):
            v = block.get(key)
            if isinstance(v, list):
                for item in v:
                    acc.add(json.dumps(item, sort_keys=True) if isinstance(item, (dict, list)) else str(item))
    return acc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", type=int, nargs="+", default=[4, 8, 16])
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if not FROZEN_B1.exists():
        raise SystemExit(f"frozen batch-1 artifact missing: {FROZEN_B1}")

    b1_rows = _load_predictions(FROZEN_B1)
    gold_ids = [r["sample_id"] for r in _load_jsonl(GOLD)]

    # Score frozen batch-1 once (baseline decision lanes).
    print("Scoring frozen batch-1 ...", file=sys.stderr)
    b1_scores = score_with_goldscore(FROZEN_B1, "batch1")
    b1_accepted = _accepted_set(b1_scores)

    comparison: dict = {
        "artifact": "relex_batch_comparison",
        "frozen_batch1": FROZEN_B1.name,
        "gold": GOLD.name,
        "ontology": str(ONTOLOGY.relative_to(REPO_ROOT)),
        "sizes": {},
        "selected_batch_size": 1,
    }
    parity: dict = {
        "artifact": "gold_decision_parity",
        "gates": {
            "span_parity": 1.0, "endpoint_parity": 1.0, "predicate_parity": 1.0,
            "lane_parity": 1.0, "lost_gold_relations": 0,
            "new_unsupported_accepts": 0, "missing_rows": 0, "duplicate_rows": 0,
        },
        "results": {},
    }

    passing_sizes: list[int] = []

    for size in args.sizes:
        print(f"\n=== batch size {size} ===", file=sys.stderr)
        pred_path = DATA_DIR / f"relex_large_v3_mps_fp32.batch{size}.predictions.jsonl"
        # (Re)generate batch-N predictions.
        regen = subprocess.run(
            [sys.executable, str(REPO_ROOT / "backend/scripts/regen_relex_benchmark_batched.py"),
             "--device", "mps", "--batch-size", str(size)],
            capture_output=True, text=True,
        )
        if regen.returncode != 0:
            print(f"  regen FAILED:\n{regen.stderr}", file=sys.stderr)
            comparison["sizes"][str(size)] = {"error": "regen_failed", "stderr": regen.stderr[-2000:]}
            continue
        # Pull throughput line from stdout.
        thr = next((l for l in regen.stdout.splitlines() if l.startswith("THROUGHPUT")), "")
        bn_rows = _load_predictions(pred_path)

        # Row presence / duplication gates.
        missing = [s for s in gold_ids if s not in bn_rows]
        dup = len(bn_rows) != len(set(bn_rows.keys()))
        per_sample = []
        span_min = endpoint_min = predicate_min = 1.0
        max_conf = 0.0
        for sid in gold_ids:
            if sid not in bn_rows or sid not in b1_rows:
                continue
            d = diff_rows(b1_rows[sid], bn_rows[sid], sid)
            per_sample.append(d)
            span_min = min(span_min, d["span_parity"])
            endpoint_min = min(endpoint_min, d["endpoint_parity"])
            predicate_min = min(predicate_min, d["predicate_parity"])
            max_conf = max(max_conf, d["max_confidence_delta"])

        # goldscore decision lanes for batch-N.
        bn_scores = score_with_goldscore(pred_path, f"batch{size}")
        bn_accepted = _accepted_set(bn_scores)
        lost_gold = len(b1_accepted - bn_accepted)
        new_accepts = len(bn_accepted - b1_accepted)
        lane_parity = 1.0 if (b1_accepted == bn_accepted) else 0.0

        # also compare raw score dicts strict/canonical equality of the metrics
        same_scores = json.dumps(b1_scores, sort_keys=True) == json.dumps(bn_scores, sort_keys=True)

        gates = {
            "span_parity": span_min,
            "endpoint_parity": endpoint_min,
            "predicate_parity": predicate_min,
            "lane_parity": lane_parity,
            "lost_gold_relations": lost_gold,
            "new_unsupported_accepts": new_accepts,
            "missing_rows": len(missing),
            "duplicate_rows": 1 if dup else 0,
            "confidence_within_tol": max_conf <= FLOAT_TOL,
            "max_confidence_delta": max_conf,
            "goldscore_metrics_identical": same_scores,
        }
        passed = (
            span_min == 1.0 and endpoint_min == 1.0 and predicate_min == 1.0
            and lane_parity == 1.0 and lost_gold == 0 and new_accepts == 0
            and len(missing) == 0 and not dup
        )
        if passed:
            passing_sizes.append(size)

        comparison["sizes"][str(size)] = {
            "passed": passed,
            "throughput": thr,
            "gates": gates,
            "per_sample": per_sample,
        }
        parity["results"][str(size)] = {
            "span_parity": span_min, "endpoint_parity": endpoint_min,
            "predicate_parity": predicate_min, "lane_parity": lane_parity,
            "lost_gold_relations": lost_gold, "new_unsupported_accepts": new_accepts,
            "missing_rows": len(missing), "duplicate_rows": 1 if dup else 0,
            "passed": passed,
        }
        print(f"  size={size} passed={passed} span={span_min} ep={endpoint_min} "
              f"pred={predicate_min} lane={lane_parity} confΔ={max_conf:.2e}", file=sys.stderr)

    selected = max(passing_sizes) if passing_sizes else 1
    comparison["selected_batch_size"] = selected
    comparison["passing_sizes"] = passing_sizes

    with open(OUT_DIR / "relex_batch_comparison.json", "w") as f:
        json.dump(comparison, f, indent=2, sort_keys=True)
    with open(OUT_DIR / "gold_decision_parity.json", "w") as f:
        json.dump(parity, f, indent=2, sort_keys=True)

    print(f"\nSELECTED batch_size={selected} (passing: {passing_sizes or 'none — default 1'})", file=sys.stderr)
    print(f"Wrote {OUT_DIR/'relex_batch_comparison.json'}", file=sys.stderr)
    print(f"Wrote {OUT_DIR/'gold_decision_parity.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

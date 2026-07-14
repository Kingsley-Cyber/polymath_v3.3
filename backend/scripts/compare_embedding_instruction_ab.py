#!/usr/bin/env python3
"""Assert the preregistered T5.6 query-instruction promotion gates.

The one changed variable is the Qwen3 query instruction. Stored vectors and
held-out denominators are frozen. Each candidate tier must satisfy:

* identical question ids and zero runtime errors;
* negative controls 5/5 fail-closed;
* naive (lay-language) AND cross_corpus mean document recall strictly improve;
* no answer-shape loses a document hit, and no shape's mean document recall
  falls by more than 5 percentage points;
* whole-query mean latency stays within +20% of baseline.

Fast is evaluated first by passing one baseline/candidate pair. Only a green
Fast result authorizes running and comparing Hybrid and Graph.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

PRIMARY_SHAPES = ("naive", "cross_corpus")
NEGATIVE_SHAPE = "negative_control"
MAX_SHAPE_RECALL_REGRESSION = 0.05
MAX_LATENCY_RATIO = 1.20
MIN_STRICT_LIFT = 0.001


def _load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data.get("results"), list) or not isinstance(data.get("summary"), dict):
        raise ValueError(f"{path}: malformed eval artifact")
    return data


def _mean(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    return sum(values) / len(values) if values else None


def _shape_metrics(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in data["results"]:
        if row.get("error"):
            continue
        buckets.setdefault(str(row["shape"]), []).append(row)
    out: dict[str, dict[str, Any]] = {}
    for shape, rows in buckets.items():
        hits = [bool(row["doc_hit"]) for row in rows if row.get("doc_hit") is not None]
        out[shape] = {
            "n": len(rows),
            "doc_hits": sum(hits),
            "doc_hit_denominator": len(hits),
            "doc_recall_mean": _mean(rows, "doc_recall"),
            "answerability_ok": sum(bool(row.get("answerability_ok")) for row in rows),
        }
    return out


def compare_pair(baseline_path: Path, candidate_path: Path) -> dict[str, Any]:
    baseline = _load(baseline_path)
    candidate = _load(candidate_path)
    baseline_tier = str(baseline["summary"].get("tier") or "")
    candidate_tier = str(candidate["summary"].get("tier") or "")
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, **details: Any) -> None:
        checks.append({"name": name, "passed": bool(passed), **details})

    check(
        "tier_identity",
        baseline_tier == candidate_tier and bool(baseline_tier),
        baseline=baseline_tier,
        candidate=candidate_tier,
    )
    baseline_ids = {str(row["id"]) for row in baseline["results"]}
    candidate_ids = {str(row["id"]) for row in candidate["results"]}
    check(
        "frozen_denominator",
        baseline_ids == candidate_ids and len(baseline_ids) == len(baseline["results"]),
        baseline_n=len(baseline_ids),
        candidate_n=len(candidate_ids),
        missing=sorted(baseline_ids - candidate_ids),
        added=sorted(candidate_ids - baseline_ids),
    )
    candidate_errors = [row["id"] for row in candidate["results"] if row.get("error")]
    check("candidate_errors_zero", not candidate_errors, errors=candidate_errors)

    base_shapes = _shape_metrics(baseline)
    cand_shapes = _shape_metrics(candidate)
    negative = cand_shapes.get(NEGATIVE_SHAPE) or {}
    check(
        "negative_controls_5_of_5",
        negative.get("n") == 5 and negative.get("answerability_ok") == 5,
        observed=f"{negative.get('answerability_ok', 0)}/{negative.get('n', 0)}",
    )

    for shape in sorted(set(base_shapes) | set(cand_shapes)):
        before = base_shapes.get(shape) or {}
        after = cand_shapes.get(shape) or {}
        check(
            f"shape_{shape}_doc_hits_nonregression",
            before.get("n") == after.get("n")
            and int(after.get("doc_hits", -1)) >= int(before.get("doc_hits", 0)),
            before=before.get("doc_hits"),
            after=after.get("doc_hits"),
            n=before.get("n"),
        )
        before_recall = before.get("doc_recall_mean")
        after_recall = after.get("doc_recall_mean")
        if before_recall is not None:
            delta = None if after_recall is None else after_recall - before_recall
            check(
                f"shape_{shape}_recall_regression_le_5pt",
                delta is not None and delta >= -MAX_SHAPE_RECALL_REGRESSION,
                before=before_recall,
                after=after_recall,
                delta=delta,
            )

    for shape in PRIMARY_SHAPES:
        before = (base_shapes.get(shape) or {}).get("doc_recall_mean")
        after = (cand_shapes.get(shape) or {}).get("doc_recall_mean")
        delta = None if before is None or after is None else after - before
        check(
            f"primary_{shape}_strict_recall_lift",
            delta is not None and delta >= MIN_STRICT_LIFT,
            before=before,
            after=after,
            delta=delta,
            minimum=MIN_STRICT_LIFT,
        )

    baseline_latency = float(baseline["summary"].get("latency_mean_s") or 0.0)
    candidate_latency = float(candidate["summary"].get("latency_mean_s") or 0.0)
    latency_ratio = candidate_latency / baseline_latency if baseline_latency else None
    check(
        "latency_within_plus_20pct",
        latency_ratio is not None and latency_ratio <= MAX_LATENCY_RATIO,
        baseline_s=baseline_latency,
        candidate_s=candidate_latency,
        ratio=latency_ratio,
    )

    passed = all(item["passed"] for item in checks)
    return {
        "tier": baseline_tier or candidate_tier,
        "baseline": str(baseline_path),
        "candidate": str(candidate_path),
        "passed": passed,
        "thresholds": {
            "primary_shapes": list(PRIMARY_SHAPES),
            "minimum_strict_recall_lift": MIN_STRICT_LIFT,
            "max_shape_recall_regression": MAX_SHAPE_RECALL_REGRESSION,
            "max_latency_ratio": MAX_LATENCY_RATIO,
            "negative_controls": "5/5",
            "shape_doc_hits": "no loss",
        },
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", action="append", required=True, type=Path)
    parser.add_argument("--candidate", action="append", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if len(args.baseline) != len(args.candidate):
        parser.error("--baseline and --candidate must be supplied in equal pairs")

    comparisons = [
        compare_pair(baseline, candidate)
        for baseline, candidate in zip(args.baseline, args.candidate)
    ]
    receipt = {
        "gate": "T5.6 universal query-instruction A/B",
        "passed": all(item["passed"] for item in comparisons),
        "comparisons": comparisons,
    }
    rendered = json.dumps(receipt, indent=2, sort_keys=True)
    print(rendered)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n")
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Compare ablation runs and produce a unified comparison artifact.

Usage:
    python compare_ablation_runs.py \
      --runs data/ablations/A data/ablations/B ... data/ablations/F \
      --output data/ablations/comparison.json

Reads manifest.json, summary.json, and results.jsonl from each run directory.
Validates that all runs share identical frozen inputs (predictions, gold,
ontology, acceptance policy). Emits:
  - Per-profile metric table
  - Adjacent-pair deltas (introduced, removed, decision changed, predicate
    changed, join mode changed)
  - Cumulative feature attribution (B−A, C−B, ...)

Exit code 1 if frozen-input validation fails.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_run(run_dir: Path) -> dict:
    """Load a single ablation run directory."""
    manifest_path = run_dir / "manifest.json"
    summary_path = run_dir / "summary.json"
    results_path = run_dir / "results.jsonl"

    if not manifest_path.exists():
        print(f"ERROR: {manifest_path} not found", file=sys.stderr)
        sys.exit(1)
    if not summary_path.exists():
        print(f"ERROR: {summary_path} not found", file=sys.stderr)
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    results: list[dict] = []
    if results_path.exists():
        with results_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    results.append(json.loads(line))

    return {"manifest": manifest, "summary": summary, "results": results}


def validate_frozen_inputs(runs: list[dict]) -> None:
    """Fail if two profiles use different frozen inputs."""
    reference = runs[0]["manifest"]
    ref_profile = reference.get("ablation_profile", "?")

    check_fields = [
        "predictions_sha256",
        "gold_artifact_sha256",
        "ontology_sha256",
        "acceptance_policy_sha256",
    ]

    for run in runs[1:]:
        m = run["manifest"]
        profile = m.get("ablation_profile", "?")
        for field in check_fields:
            if m.get(field) != reference.get(field):
                print(
                    f"FATAL: frozen input mismatch between profile "
                    f"{ref_profile} and {profile} on field '{field}':\n"
                    f"  {ref_profile}: {reference.get(field)}\n"
                    f"  {profile}: {m.get(field)}\n"
                    f"All profiles must use identical predictions, gold, "
                    f"ontology, and acceptance policy.",
                    file=sys.stderr,
                )
                sys.exit(1)


def candidate_key(r: dict) -> str:
    """Stable identity key for a candidate pair."""
    return f"{r.get('sid', '')}:{r.get('subject', '')}:{r.get('object', '')}"


def compute_delta(prev_results: list[dict], curr_results: list[dict]) -> dict:
    """Compute candidate-level delta between adjacent profiles."""
    prev_by_key = {candidate_key(r): r for r in prev_results}
    curr_by_key = {candidate_key(r): r for r in curr_results}

    prev_keys = set(prev_by_key.keys())
    curr_keys = set(curr_by_key.keys())

    introduced = curr_keys - prev_keys
    removed = prev_keys - curr_keys
    common = prev_keys & curr_keys

    decision_changed = []
    predicate_changed = []
    join_mode_changed = []

    for key in common:
        p = prev_by_key[key]
        c = curr_by_key[key]
        if p.get("status") != c.get("status"):
            decision_changed.append({
                "key": key,
                "prev_status": p.get("status"),
                "curr_status": c.get("status"),
            })
        if p.get("predicate") != c.get("predicate"):
            predicate_changed.append({
                "key": key,
                "prev_predicate": p.get("predicate"),
                "curr_predicate": c.get("predicate"),
            })
        if p.get("join_mode") != c.get("join_mode"):
            join_mode_changed.append({
                "key": key,
                "prev_join_mode": p.get("join_mode"),
                "curr_join_mode": c.get("join_mode"),
            })

    return {
        "introduced_count": len(introduced),
        "removed_count": len(removed),
        "decision_changed_count": len(decision_changed),
        "predicate_changed_count": len(predicate_changed),
        "join_mode_changed_count": len(join_mode_changed),
        "introduced": sorted(introduced)[:50],  # cap for readability
        "removed": sorted(removed)[:50],
        "decision_changed": decision_changed[:50],
        "predicate_changed": predicate_changed[:50],
        "join_mode_changed": join_mode_changed[:50],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare ablation runs and produce comparison.json"
    )
    parser.add_argument(
        "--runs", nargs="+", required=True, type=Path,
        help="Ordered run directories (A B C D E F)",
    )
    parser.add_argument(
        "--output", required=True, type=Path,
        help="Output path for comparison.json",
    )
    args = parser.parse_args()

    runs = [load_run(d) for d in args.runs]
    validate_frozen_inputs(runs)

    # --- Per-profile metric table ---
    profiles_table = []
    for run in runs:
        s = run["summary"]
        m = run["manifest"]
        profiles_table.append({
            "profile": m.get("ablation_profile", "?"),
            "features": m.get("features", {}),
            "total_pairs": s.get("total_pairs", 0),
            "accepted_count": s.get("accepted_count", 0),
            "shadow_count": s.get("shadow_count", 0),
            "review_count": s.get("review_count", 0),
            "scope_rejection_count": s.get("scope_rejection_count", 0),
            "open_relation_count": s.get("open_relation_count", 0),
            "correct_accepted": s.get("correct_accepted", 0),
            "gold_total": s.get("gold_total", 0),
            "gold_matched": s.get("gold_matched", 0),
            "gold_pair_recall": s.get("gold_pair_recall", 0.0),
            "accepted_labeled_hit_rate": s.get("accepted_labeled_hit_rate", 0.0),
        })

    # --- Adjacent deltas ---
    deltas = []
    for i in range(1, len(runs)):
        prev_profile = runs[i - 1]["manifest"].get("ablation_profile", "?")
        curr_profile = runs[i]["manifest"].get("ablation_profile", "?")
        delta = compute_delta(runs[i - 1]["results"], runs[i]["results"])
        delta["from_profile"] = prev_profile
        delta["to_profile"] = curr_profile
        # Feature attribution label
        feature_names = [
            "head_token_join", "open_relation_lane", "credit_patterns",
            "verb_prep_frames", "typed_cross_sentence_links",
        ]
        prev_feat = runs[i - 1]["manifest"].get("features", {})
        curr_feat = runs[i]["manifest"].get("features", {})
        activated = [
            f for f in feature_names
            if curr_feat.get(f) and not prev_feat.get(f)
        ]
        delta["activated_feature"] = activated[0] if activated else "none"
        deltas.append(delta)

    # --- Comparison artifact ---
    comparison = {
        "profiles": profiles_table,
        "deltas": deltas,
        "frozen_inputs": {
            "predictions_sha256": runs[0]["manifest"].get("predictions_sha256"),
            "gold_artifact_sha256": runs[0]["manifest"].get("gold_artifact_sha256"),
            "ontology_sha256": runs[0]["manifest"].get("ontology_sha256"),
            "acceptance_policy_sha256": runs[0]["manifest"].get("acceptance_policy_sha256"),
            "code_commit": runs[0]["manifest"].get("code_commit"),
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(comparison, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # --- Print summary table ---
    print(f"\n{'Profile':<8} {'Accepted':>9} {'Shadow':>7} {'Review':>7} "
          f"{'ScopeRej':>9} {'OpenRel':>8} {'GoldRecall':>11} {'HitRate':>8}")
    print("-" * 80)
    for row in profiles_table:
        print(f"{row['profile']:<8} {row['accepted_count']:>9} "
              f"{row['shadow_count']:>7} {row['review_count']:>7} "
              f"{row['scope_rejection_count']:>9} "
              f"{row['open_relation_count']:>8} "
              f"{row['gold_pair_recall']:>11.4f} "
              f"{row['accepted_labeled_hit_rate']:>8.4f}")

    print(f"\nDeltas:")
    for d in deltas:
        print(f"  {d['from_profile']}→{d['to_profile']} "
              f"(+{d['activated_feature']}): "
              f"introduced={d['introduced_count']} "
              f"removed={d['removed_count']} "
              f"decision_changed={d['decision_changed_count']} "
              f"predicate_changed={d['predicate_changed_count']} "
              f"join_mode_changed={d['join_mode_changed_count']}")

    print(f"\nComparison written to: {args.output}")


if __name__ == "__main__":
    main()

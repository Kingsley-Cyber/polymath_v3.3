#!/usr/bin/env python3
"""P1B: Acceptance-precision audit and dual trace system.

Produces:
  1. Gold failure traces — for every gold relation, full stage-by-stage trace
  2. Accepted-candidate traces — for every accepted pair, full diagnostics
  3. Precision breakdown — by lane, predicate, scope, score bucket
  4. Gold exhaustiveness report

Usage:
  cd backend && ../.venv-relex/bin/python scripts/p1b_precision_audit.py [--json]
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from services.extraction.corroboration_gate import (
    GateStatus,
    evaluate_relation,
    load_policy,
)
from services.extraction.frame_extractor import FrameExtractor
from services.extraction.relation_evidence import GateDecision

# Import from the unified pipeline (same directory)
sys.path.insert(0, str(BACKEND / "scripts"))
from unified_shadow_pipeline import (
    build_union_evidence,
    generate_unified_syntax,
    load_jsonl,
    _gold_relations_for_sample,
)

GOLD_PATH = REPO_ROOT / "data/deterministic_gold_score/data/relex_gold_v1.jsonl"
PRED_PATH = REPO_ROOT / "data/deterministic_gold_score/data/relex_large_v2.predictions.jsonl"
ENDPOINT_TYPES_PATH = REPO_ROOT / "data/deterministic_gold_score/data/endpoint_type_annotations.json"


def _norm(s: str) -> str:
    return " ".join(str(s).lower().split()).strip(".,;:'\"()[]")


def _near(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    return a == b or a in b or b in a


def main() -> int:
    json_mode = "--json" in sys.argv

    gold_samples = {s["sample_id"]: s for s in load_jsonl(GOLD_PATH)}
    predictions = {p["sample_id"]: p for p in load_jsonl(PRED_PATH)}
    endpoint_types = json.loads(ENDPOINT_TYPES_PATH.read_text())

    policy = load_policy()
    extractor = FrameExtractor()

    # --- Gold exhaustiveness report ---
    closed_world_flags = [g.get("closed_world", None) for g in gold_samples.values()]
    any_closed = any(closed_world_flags)
    all_open = all(f is False for f in closed_world_flags)

    print("=" * 80)
    print("P1B ACCEPTANCE-PRECISION AUDIT")
    print("=" * 80)
    print(f"\nGold samples: {len(gold_samples)}")
    print(f"closed_world flags: {Counter(closed_world_flags)}")
    if all_open:
        print("VERDICT: Gold is NON-EXHAUSTIVE (closed_world=False for all samples)")
        print("  → 0.030 'precision' is actually accepted_labeled_hit_rate")
        print("  → Cannot classify unannotated predictions as false positives")
    print()

    # --- Run pipeline per sample, collect all gate decisions ---
    all_decisions: list[dict] = []
    gold_traces: list[dict] = []

    # Collect gold relations
    gold_relations = []
    for sid, sample in gold_samples.items():
        ent_map = {e["entity_id"]: e for e in sample.get("entities", [])}
        for rel in sample.get("relations", []):
            h = ent_map.get(rel["head_id"], {})
            t = ent_map.get(rel["tail_id"], {})
            gold_relations.append({
                "sample_id": sid,
                "predicate": rel["predicate"],
                "head_text": h.get("text", ""),
                "head_start": h.get("start", -1),
                "head_end": h.get("end", -1),
                "tail_text": t.get("text", ""),
                "tail_start": t.get("start", -1),
                "tail_end": t.get("end", -1),
            })

    # Build endpoint type lookup
    ep_type_lookup = {
        a["surface"].lower(): a["mention_type"]
        for a in endpoint_types.get("annotations", [])
    }

    for sid, sample in gold_samples.items():
        pred = predictions.get(sid)
        if pred is None:
            continue

        text = sample["text"]
        raw_entities = pred.get("entities", [])

        # Generate syntax evidence (same as unified pipeline)
        resolved_syntax, unmapped_syntax = generate_unified_syntax(
            text, raw_entities, sid, extractor,
        )

        # Build union evidence
        union_evidence = build_union_evidence(
            sid, pred, resolved_syntax, unmapped_syntax, text=text,
        )

        # Evaluate each pair in the union
        for ev in union_evidence:
            decision = evaluate_relation(ev, policy)
            # Extract syntax predicate from evidence tuple
            syn_pred = ""
            if ev.syntax_evidence:
                syn_pred = ev.syntax_evidence[0].surface_predicate if ev.syntax_evidence else ""
            all_decisions.append({
                "sample_id": sid,
                "subject": ev.subject_text,
                "object": ev.object_text,
                "subject_type": ev.subject_type,
                "object_type": ev.object_type,
                "predicate": decision.predicate,
                "score": decision.score,
                "margin": decision.margin,
                "direction_margin": decision.direction_margin,
                "status": decision.status.value,
                "reasons": list(decision.reasons),
                "same_sentence": ev.same_sentence,
                "has_syntax": len(ev.syntax_evidence) > 0,
                "syntax_predicate": syn_pred,
                "type_compatibility": _type_compat(policy, decision.predicate, ev.subject_type, ev.object_type),
            })

    # --- Gold failure traces ---
    print("=" * 80)
    print("GOLD FAILURE TRACES (18 relations)")
    print("=" * 80)

    for gr in gold_relations:
        sid = gr["sample_id"]
        # Find matching decision
        match = None
        for d in all_decisions:
            if d["sample_id"] != sid:
                continue
            if (_near(d["subject"], gr["head_text"]) and _near(d["object"], gr["tail_text"])):
                if d["predicate"] == gr["predicate"] or match is None:
                    match = d
                    if d["predicate"] == gr["predicate"]:
                        break

        # Determine primary failure
        if match is None:
            primary_failure = "PAIR_NOT_FOUND"
            trace = {
                "gold_id": f"{sid}:{gr['head_text']}→{gr['predicate']}→{gr['tail_text']}",
                "primary_failure": "PAIR_NOT_FOUND",
                "blocking_reasons": ["ENTITY_ALIGNMENT_FAILED"],
                "final_decision": "NOT_IN_UNION",
            }
        else:
            status = match["status"]
            blockers = []
            if not match["same_sentence"]:
                blockers.append("CROSS_SENTENCE")
            if match["subject_type"] in ("unknown", ""):
                blockers.append("SUBJECT_TYPE_UNKNOWN")
            if match["object_type"] in ("unknown", ""):
                blockers.append("OBJECT_TYPE_UNKNOWN")
            if not match["has_syntax"]:
                blockers.append("NO_SYNTAX_SUPPORT")
            if match["type_compatibility"] == "invalid":
                blockers.append("ENDPOINT_SIGNATURE_INVALID")
            elif match["type_compatibility"] == "ambiguous":
                blockers.append("ENDPOINT_SIGNATURE_AMBIGUOUS")
            if match["score"] < 0.30:
                blockers.append("SCORE_BELOW_THRESHOLD")
            if match["margin"] < 0.05:
                blockers.append("PREDICATE_MARGIN_LOW")

            # Map status to primary failure
            failure_map = {
                "accept_high": "NONE_ACCEPTED",
                "accept_corroborated": "NONE_ACCEPTED",
                "shadow_relex_high": "NONE_SHADOW",
                "review_cross_sentence": "SCOPE_BLOCKED",
                "review_unknown_type": "TYPE_UNKNOWN",
                "review_type_compatibility": "TYPE_AMBIGUOUS",
                "review_conflict": "PREDICATE_DISCRIMINATION",
                "reject_endpoint_signature": "ENDPOINT_SIGNATURE_FAILURE",
                "reject_negated": "NEGATION_DETECTED",
                "reject_low_evidence": "SCORE_FAILURE",
                "reject_scope": "SCOPE_BLOCKED",
            }
            primary_failure = failure_map.get(status, "UNKNOWN")

            trace = {
                "gold_id": f"{sid}:{gr['head_text']}→{gr['predicate']}→{gr['tail_text']}",
                "entity_detection": {
                    "subject_found": True,
                    "object_found": True,
                    "subject_type": match["subject_type"],
                    "object_type": match["object_type"],
                    "subject_endpoint_type": ep_type_lookup.get(gr["head_text"].lower(), "?"),
                    "object_endpoint_type": ep_type_lookup.get(gr["tail_text"].lower(), "?"),
                },
                "relex_stage": {
                    "gold_predicate": gr["predicate"],
                    "gate_predicate": match["predicate"],
                    "gold_score": round(match["score"], 4),
                    "predicate_margin": round(match["margin"], 4),
                    "direction_margin": round(match["direction_margin"], 4),
                    "predicate_match": match["predicate"] == gr["predicate"],
                },
                "syntax_stage": {
                    "parse_available": match["has_syntax"],
                    "surface_predicate": match["syntax_predicate"] or None,
                },
                "type_stage": {
                    "compatibility": match["type_compatibility"],
                },
                "scope": "SAME_SENTENCE" if match["same_sentence"] else "CROSS_SENTENCE",
                "primary_failure": primary_failure,
                "blocking_reasons": blockers,
                "final_decision": status,
            }

        gold_traces.append(trace)
        status_icon = "✓" if trace.get("final_decision", "").startswith("accept") else "✗"
        print(f"  [{status_icon}] {trace['gold_id']}")
        print(f"      primary_failure={trace['primary_failure']}  decision={trace.get('final_decision', '?')}")
        if trace.get("blocking_reasons"):
            print(f"      blockers={trace['blocking_reasons']}")

    # --- Accepted-candidate traces ---
    accepted = [d for d in all_decisions if d["status"].startswith("accept") or d["status"] == "shadow_relex_high"]
    print(f"\n{'=' * 80}")
    print(f"ACCEPTED-CANDIDATE TRACES ({len(accepted)} candidates)")
    print("=" * 80)

    # Check which accepted match gold
    for d in accepted:
        gold_match = None
        for gr in gold_relations:
            if d["sample_id"] == gr["sample_id"]:
                if _near(d["subject"], gr["head_text"]) and _near(d["object"], gr["tail_text"]):
                    gold_match = gr
                    break
        d["gold_match"] = gold_match is not None
        d["risk_flags"] = []
        if not d["has_syntax"]:
            d["risk_flags"].append("NO_SYNTAX_SUPPORT")
        if d["type_compatibility"] == "ambiguous":
            d["risk_flags"].append("AMBIGUOUS_ENDPOINT_SIGNATURE")
        if d["margin"] < 0.05:
            d["risk_flags"].append("LOW_PREDICATE_MARGIN")
        if d["direction_margin"] < 0.05:
            d["risk_flags"].append("LOW_DIRECTION_MARGIN")
        if not d["same_sentence"]:
            d["risk_flags"].append("CROSS_SENTENCE")

    # --- Precision breakdown ---
    print(f"\n{'=' * 80}")
    print("PRECISION BREAKDOWN (gold is NON-EXHAUSTIVE → 'correct' = labeled match)")
    print("=" * 80)

    # By lane
    lanes = defaultdict(lambda: {"accepted": 0, "labeled_match": 0})
    for d in accepted:
        lane = d["status"]
        lanes[lane]["accepted"] += 1
        if d["gold_match"]:
            lanes[lane]["labeled_match"] += 1

    print(f"\n{'Lane':<30} {'Accepted':>8} {'Labeled':>8} {'Hit Rate':>9}")
    print("-" * 58)
    for lane in sorted(lanes):
        l = lanes[lane]
        hr = l["labeled_match"] / max(1, l["accepted"])
        print(f"{lane:<30} {l['accepted']:>8} {l['labeled_match']:>8} {hr:>9.3f}")

    # By predicate
    print(f"\n{'Predicate':<20} {'Accepted':>8} {'Labeled':>8} {'Hit Rate':>9}")
    print("-" * 48)
    pred_counts = defaultdict(lambda: {"accepted": 0, "labeled_match": 0})
    for d in accepted:
        p = d["predicate"] or "NONE"
        pred_counts[p]["accepted"] += 1
        if d["gold_match"]:
            pred_counts[p]["labeled_match"] += 1
    for p in sorted(pred_counts, key=lambda x: -pred_counts[x]["accepted"]):
        c = pred_counts[p]
        hr = c["labeled_match"] / max(1, c["accepted"])
        print(f"{p:<20} {c['accepted']:>8} {c['labeled_match']:>8} {hr:>9.3f}")

    # By scope
    print(f"\n{'Scope':<20} {'Accepted':>8} {'Labeled':>8} {'Hit Rate':>9}")
    print("-" * 48)
    scope_counts = defaultdict(lambda: {"accepted": 0, "labeled_match": 0})
    for d in accepted:
        scope = "SAME_SENTENCE" if d["same_sentence"] else "CROSS_SENTENCE"
        scope_counts[scope]["accepted"] += 1
        if d["gold_match"]:
            scope_counts[scope]["labeled_match"] += 1
    for s in sorted(scope_counts):
        c = scope_counts[s]
        hr = c["labeled_match"] / max(1, c["accepted"])
        print(f"{s:<20} {c['accepted']:>8} {c['labeled_match']:>8} {hr:>9.3f}")

    # By syntax presence
    print(f"\n{'Syntax':<20} {'Accepted':>8} {'Labeled':>8} {'Hit Rate':>9}")
    print("-" * 48)
    syn_counts = defaultdict(lambda: {"accepted": 0, "labeled_match": 0})
    for d in accepted:
        syn = "WITH_SYNTAX" if d["has_syntax"] else "NO_SYNTAX"
        syn_counts[syn]["accepted"] += 1
        if d["gold_match"]:
            syn_counts[syn]["labeled_match"] += 1
    for s in sorted(syn_counts):
        c = syn_counts[s]
        hr = c["labeled_match"] / max(1, c["accepted"])
        print(f"{s:<20} {c['accepted']:>8} {c['labeled_match']:>8} {hr:>9.3f}")

    # By score bucket
    print(f"\n{'Score Bucket':<20} {'Accepted':>8} {'Labeled':>8} {'Hit Rate':>9}")
    print("-" * 48)
    score_buckets = defaultdict(lambda: {"accepted": 0, "labeled_match": 0})
    for d in accepted:
        s = d["score"]
        if s >= 0.8:
            bucket = "0.80-1.00"
        elif s >= 0.6:
            bucket = "0.60-0.80"
        elif s >= 0.4:
            bucket = "0.40-0.60"
        else:
            bucket = "0.30-0.40"
        score_buckets[bucket]["accepted"] += 1
        if d["gold_match"]:
            score_buckets[bucket]["labeled_match"] += 1
    for b in sorted(score_buckets):
        c = score_buckets[b]
        hr = c["labeled_match"] / max(1, c["accepted"])
        print(f"{b:<20} {c['accepted']:>8} {c['labeled_match']:>8} {hr:>9.3f}")

    # By type compatibility
    print(f"\n{'Type Compat':<20} {'Accepted':>8} {'Labeled':>8} {'Hit Rate':>9}")
    print("-" * 48)
    tc_counts = defaultdict(lambda: {"accepted": 0, "labeled_match": 0})
    for d in accepted:
        tc = d["type_compatibility"]
        tc_counts[tc]["accepted"] += 1
        if d["gold_match"]:
            tc_counts[tc]["labeled_match"] += 1
    for t in sorted(tc_counts):
        c = tc_counts[t]
        hr = c["labeled_match"] / max(1, c["accepted"])
        print(f"{t:<20} {c['accepted']:>8} {c['labeled_match']:>8} {hr:>9.3f}")

    # Summary
    total_accepted = len(accepted)
    total_labeled = sum(1 for d in accepted if d["gold_match"])
    print(f"\n{'=' * 80}")
    print(f"SUMMARY")
    print(f"  Total accepted: {total_accepted}")
    print(f"  Labeled matches: {total_labeled}")
    print(f"  accepted_labeled_hit_rate: {total_labeled / max(1, total_accepted):.3f}")
    print(f"  Gold is NON-EXHAUSTIVE → this is NOT precision")
    print(f"  Risk flags distribution:")
    risk_counter = Counter()
    for d in accepted:
        for rf in d["risk_flags"]:
            risk_counter[rf] += 1
    for rf, cnt in risk_counter.most_common():
        print(f"    {rf}: {cnt}/{total_accepted} ({100*cnt/max(1,total_accepted):.1f}%)")

    # JSON output
    if json_mode:
        output = {
            "gold_exhaustive": not all_open,
            "gold_traces": gold_traces,
            "accepted_traces": accepted[:50],  # sample
            "precision_by_lane": dict(lanes),
            "precision_by_predicate": dict(pred_counts),
            "total_accepted": total_accepted,
            "total_labeled_matches": total_labeled,
        }
        out_path = REPO_ROOT / "data/p1b_precision_audit.json"
        out_path.write_text(json.dumps(output, indent=2, default=str))
        print(f"\n  JSON output: {out_path}")

    return 0


def _type_compat(policy, predicate: str | None, subj_type: str, obj_type: str) -> str:
    if predicate is None:
        return "unknown"
    pp = policy[predicate]
    return pp.type_compatibility(subj_type, obj_type)


if __name__ == "__main__":
    raise SystemExit(main())

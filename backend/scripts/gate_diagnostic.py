#!/usr/bin/env python3
"""Gate-reason diagnostic for every gold relation.

For each gold triple, prints the full gate evaluation trace:
  gold_triple, gate_status, gate_reasons, top/second predicate+score,
  predicate_margin, reverse_score, direction_margin, type_status,
  syntax_present, syntax_predicate, join_mode, join_trust.

Usage:
    cd backend && python scripts/gate_diagnostic.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.extraction.corroboration_gate import evaluate_relation, load_policy
from services.extraction.frame_extractor import FrameExtractor
from services.extraction.relation_evidence import (
    GateStatus,
    RelationEvidence,
)

from scripts.unified_shadow_pipeline import (
    GOLD_PATH,
    PRED_PATH,
    load_jsonl,
    generate_unified_syntax,
    build_union_evidence,
    _gold_relations_for_sample,
    _matches_gold,
)


def _find_matching_evidence(
    union: list[RelationEvidence],
    gold_rel: dict,
) -> RelationEvidence | None:
    """Find the BEST union evidence matching a gold relation.

    Among all matching pairs, returns the one with the highest top-1
    predicate score.  This avoids the first-match bias where a low-scoring
    substring match (e.g. "story" for "A CHRISTMAS STORY") shadows the
    correct full-surface pair.
    """
    best: RelationEvidence | None = None
    best_score = -1.0
    for ev in union:
        if _matches_gold(ev, gold_rel):
            ranked = sorted(ev.predicate_scores, key=lambda p: (-p.score, p.predicate))
            top_score = ranked[0].score if ranked else 0.0
            if top_score > best_score:
                best_score = top_score
                best = ev
    return best


def _syntax_summary(ev: RelationEvidence) -> dict:
    if not ev.syntax_evidence:
        return {
            "syntax_present": False,
            "syntax_count": 0,
        }
    items = []
    for s in ev.syntax_evidence:
        items.append({
            "canonical": s.canonical_predicate or "(empty)",
            "surface": s.surface_predicate or "(empty)",
            "pattern": s.pattern_id,
            "join_mode": s.join_mode,
            "confidence": s.confidence,
            "negated": s.negated,
        })
    return {
        "syntax_present": True,
        "syntax_count": len(items),
        "items": items,
    }


def _score_summary(ev: RelationEvidence) -> dict:
    ranked = sorted(ev.predicate_scores, key=lambda p: (-p.score, p.predicate))
    if not ranked:
        return {"num_predicates": 0}
    top = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    return {
        "num_predicates": len(ranked),
        "top_predicate": top.predicate,
        "top_score": round(top.score, 4),
        "second_predicate": second.predicate if second else None,
        "second_score": round(second.score, 4) if second else None,
        "predicate_margin": round(top.score - (second.score if second else 0.0), 4),
    }


def _direction_summary(ev: RelationEvidence) -> dict:
    ranked = sorted(ev.predicate_scores, key=lambda p: (-p.score, p.predicate))
    if not ranked:
        return {}
    top = ranked[0]
    reverse = ev.reverse_scores.get(top.predicate, 0.0)
    return {
        "reverse_score": round(reverse, 4),
        "direction_margin": round(top.score - reverse, 4),
    }


def main() -> int:
    gold_samples = {s["sample_id"]: s for s in load_jsonl(GOLD_PATH)}
    predictions = {p["sample_id"]: p for p in load_jsonl(PRED_PATH)}
    policy = load_policy()
    extractor = FrameExtractor()

    print("=" * 90)
    print("GATE-REASON DIAGNOSTIC — every gold relation")
    print("=" * 90)

    total = 0
    found = 0
    not_found = 0
    accepted = 0
    review_unknown = 0
    review_cross = 0
    rejected = 0
    miss_reasons: dict[str, int] = {}

    for sid, sample in sorted(gold_samples.items()):
        pred = predictions.get(sid)
        if pred is None:
            continue

        text = sample["text"]
        raw_entities = pred.get("entities", [])
        gold_rels = _gold_relations_for_sample(sample)

        resolved_syntax, unmapped_syntax = generate_unified_syntax(
            text, raw_entities, sid, extractor,
        )
        union = build_union_evidence(sid, pred, resolved_syntax, unmapped_syntax, text=text)

        for gr in gold_rels:
            total += 1
            triple = f"{gr['head_text']} → {gr['predicate']} → {gr['tail_text']}"

            ev = _find_matching_evidence(union, gr)
            if ev is None:
                not_found += 1
                miss_reasons["NOT_IN_UNION"] = miss_reasons.get("NOT_IN_UNION", 0) + 1
                print(f"\n[NOT FOUND] {triple}")
                print(f"  sample: {sid}")
                # Check if entities exist in union at all
                gh = gr["head_text"].lower().strip()
                gt = gr["tail_text"].lower().strip()
                head_in = any(e.subject_text.lower().strip() == gh for e in union)
                tail_in = any(e.object_text.lower().strip() == gt for e in union)
                print(f"  head_in_union: {head_in}  tail_in_union: {tail_in}")
                continue

            found += 1
            decision = evaluate_relation(ev, policy)
            scores = _score_summary(ev)
            direction = _direction_summary(ev)
            syntax = _syntax_summary(ev)

            is_acc = decision.status in (
                GateStatus.ACCEPT_HIGH,
                GateStatus.ACCEPT_CORROBORATED,
            )
            is_review = decision.status == GateStatus.REVIEW_UNKNOWN_TYPE
            is_cross = decision.status == GateStatus.REVIEW_CROSS_SENTENCE
            if is_acc:
                tag = "ACCEPTED"
                accepted += 1
            elif is_review:
                tag = "REVIEW_UNKNOWN_TYPE"
                review_unknown += 1
            elif is_cross:
                tag = "REVIEW_CROSS_SENTENCE"
                review_cross += 1
            else:
                tag = "REJECTED"
                rejected += 1
                reason_key = decision.status.value
                miss_reasons[reason_key] = miss_reasons.get(reason_key, 0) + 1

            print(f"\n[{tag}] {triple}")
            print(f"  sample: {sid}")
            print(f"  gate_status: {decision.status.value}")
            print(f"  gate_reasons: {list(decision.reasons)}")
            print(f"  gate_predicate: {decision.predicate}")
            print(f"  gate_score: {decision.score}")
            print(f"  gate_margin: {decision.margin}")
            print(f"  gate_direction_margin: {decision.direction_margin}")

            print(f"  --- scores ---")
            print(f"  top_predicate: {scores.get('top_predicate')}")
            print(f"  top_score: {scores.get('top_score')}")
            print(f"  second_predicate: {scores.get('second_predicate')}")
            print(f"  second_score: {scores.get('second_score')}")
            print(f"  predicate_margin: {scores.get('predicate_margin')}")
            print(f"  num_predicates: {scores.get('num_predicates')}")

            print(f"  --- direction ---")
            print(f"  reverse_score: {direction.get('reverse_score')}")
            print(f"  direction_margin: {direction.get('direction_margin')}")

            print(f"  --- types ---")
            print(f"  subject_type: {ev.subject_type or '(empty)'}")
            print(f"  object_type: {ev.object_type or '(empty)'}")

            print(f"  --- syntax ---")
            print(f"  syntax_present: {syntax['syntax_present']}")
            print(f"  syntax_count: {syntax['syntax_count']}")
            if syntax.get("items"):
                for i, item in enumerate(syntax["items"]):
                    print(f"    [{i}] canonical={item['canonical']}  surface={item['surface']}  "
                          f"join={item['join_mode']}  conf={item['confidence']}  neg={item['negated']}  "
                          f"pattern={item['pattern']}")

            # Threshold analysis for Relex-anchored pairs
            if scores.get("top_score") is not None:
                pred_policy = policy[decision.predicate or scores["top_predicate"]]
                std_t = pred_policy.standard_threshold
                std_m = pred_policy.standard_margin
                dir_m = pred_policy.direction_margin
                cor_t = pred_policy.corroborated_threshold
                cor_m = pred_policy.corroborated_margin

                s_pass = scores["top_score"] >= std_t
                m_pass = (scores.get("predicate_margin") or 0) >= std_m
                d_pass = (direction.get("direction_margin") or 0) >= dir_m

                print(f"  --- threshold analysis (predicate={decision.predicate or scores['top_predicate']}) ---")
                print(f"  standard: score>={std_t} {'PASS' if s_pass else 'FAIL'}  "
                      f"margin>={std_m} {'PASS' if m_pass else 'FAIL'}  "
                      f"dir_margin>={dir_m} {'PASS' if d_pass else 'FAIL'}")
                print(f"  standard_gate_would_pass: {s_pass and m_pass and d_pass}")

                if syntax.get("items"):
                    syntax_agrees = any(
                        item["canonical"] == (decision.predicate or scores.get("top_predicate"))
                        and not item["negated"]
                        for item in syntax["items"]
                    )
                    cs_pass = scores["top_score"] >= cor_t
                    cm_pass = (scores.get("predicate_margin") or 0) >= cor_m
                    print(f"  corroborated: syntax_agrees={syntax_agrees}  "
                          f"score>={cor_t} {'PASS' if cs_pass else 'FAIL'}  "
                          f"margin>={cor_m} {'PASS' if cm_pass else 'FAIL'}  "
                          f"dir_margin>={dir_m} {'PASS' if d_pass else 'FAIL'}")
                    print(f"  corroborated_gate_would_pass: {syntax_agrees and cs_pass and cm_pass and d_pass}")

    print(f"\n{'=' * 90}")
    print(f"SUMMARY: {total} gold relations, {found} found in union, {not_found} not found")
    print(f"  accepted: {accepted}, review_unknown_type: {review_unknown}, "
          f"review_cross_sentence: {review_cross}, rejected: {rejected}")
    print(f"\n  Rejection reasons:")
    for reason, count in sorted(miss_reasons.items(), key=lambda x: -x[1]):
        print(f"    {reason}: {count}")
    print(f"{'=' * 90}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

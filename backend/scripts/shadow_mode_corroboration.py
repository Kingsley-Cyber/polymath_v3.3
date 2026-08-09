#!/usr/bin/env python3
"""Shadow-mode evaluation of the corroboration gate on 18 gold relations.

For each gold relation:
1. Load the Relex raw pair scores from the predictions JSONL
2. Find ALL mentions of gold entity surfaces in the chunk text
3. Run spaCy DependencyMatcher + SVO on the text with all mentions
4. Join syntax evidence to Relex evidence (exact span → morphological → surface)
5. Run the corroboration gate
6. Report the decision for each gold triple

This proves the bridge works without modifying production.

KEY INSIGHT: gold entity annotations reference ONE occurrence of each entity
(often the first), but the syntactic relation may be expressed at a DIFFERENT
occurrence. Example: gold CAPTCHA is at position 62 (sentence 1), but the
"Companies use CAPTCHA" relation is in sentence 2 at a different CAPTCHA
position. We find all mentions and join by surface form when spans differ.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

# Ensure backend/ is on the path
REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from services.extraction.relation_evidence import (
    GateStatus,
    PredicateScore,
    RelationEvidence,
    SyntaxEvidence,
)
from services.extraction.corroboration_gate import evaluate_relation, load_policy
from services.extraction.relex_adapter import (
    build_relation_evidence,
    canonicalize_predicate_label,
    join_syntax_evidence,
)
from services.extraction.mention_normalizer import (
    is_morphological_variant,
    normalized_mention_cached,
)
from services.extraction.dep_path_extractor import (
    DepPathExtractor,
    EntitySpan,
    resolve_predicate,
)
from services.extraction.svo_candidates import svo_candidates

GOLD_PATH = REPO_ROOT / "data/deterministic_gold_score/data/relex_gold_v1.jsonl"
PRED_PATH = REPO_ROOT / "data/deterministic_gold_score/data/relex_large_v1.predictions.jsonl"


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# Mention discovery: find ALL occurrences of a surface form in text
# ---------------------------------------------------------------------------

_BOUNDARY_CHARS = frozenset(" \n\t\r.,;:!?()[]{}\"'""''*—-_/~")


def find_all_mentions(text: str, surface: str) -> list[tuple[int, int]]:
    """Find all word-boundary-delimited occurrences of surface in text.

    Case-insensitive. Returns (start, end) char offset pairs.
    """
    if not surface or not text:
        return []
    mentions: list[tuple[int, int]] = []
    surface_lower = surface.lower()
    text_lower = text.lower()
    slen = len(surface_lower)
    start = 0
    while True:
        idx = text_lower.find(surface_lower, start)
        if idx == -1:
            break
        # Check word boundaries
        before = text[idx - 1] if idx > 0 else " "
        after_pos = idx + slen
        after = text[after_pos] if after_pos < len(text) else " "
        if before in _BOUNDARY_CHARS and after in _BOUNDARY_CHARS:
            mentions.append((idx, idx + slen))
        start = idx + 1
    return mentions


def build_all_mentions(
    text: str,
    gold_entities: list[dict],
) -> list[EntitySpan]:
    """Build a comprehensive entity span list from ALL surface mentions.

    Entity types are left EMPTY because gold data uses the generic placeholder
    "concept" which doesn't match the ontology's capitalized type names
    (Organization, Software, etc.). Empty types bypass the dep_path_extractor's
    pair_allowed gate — the corroboration gate's own policy handles type checks.
    """
    seen_spans: set[tuple[int, int]] = set()
    all_spans: list[EntitySpan] = []

    for ent in gold_entities:
        surface = ent.get("text", "")
        for start, end in find_all_mentions(text, surface):
            if (start, end) not in seen_spans:
                seen_spans.add((start, end))
                all_spans.append(EntitySpan(
                    surface=text[start:end],
                    start_char=start,
                    end_char=end,
                    entity_type="",  # empty: bypass ontology pair_allowed
                ))

    # Always include the exact gold spans even if mention search missed them
    for ent in gold_entities:
        s = ent.get("start", -1)
        e = ent.get("end", -1)
        if s >= 0 and e > s and (s, e) not in seen_spans:
            seen_spans.add((s, e))
            all_spans.append(EntitySpan(
                surface=ent.get("text", text[s:e]),
                start_char=s,
                end_char=e,
                entity_type="",
            ))

    all_spans.sort(key=lambda es: es.start_char)
    return all_spans


# ---------------------------------------------------------------------------
# Syntax evidence generation: dep_path + SVO
# ---------------------------------------------------------------------------


def generate_syntax_for_chunk(
    text: str,
    gold_entities: list[dict],
    chunk_id: str,
    extractor: DepPathExtractor,
) -> list[dict]:
    """Run dep-path + SVO extraction on ALL mentions of gold entity surfaces.

    Returns syntax records with subject_text/object_text so the joiner can
    match by surface form when exact spans differ (cross-mention evidence).
    """
    all_mentions = build_all_mentions(text, gold_entities)

    records: list[dict] = []

    # --- Source 1: DependencyMatcher + dep-path triples ---
    try:
        triples = extractor.extract(
            text=text,
            entities=all_mentions,
            chunk_id=chunk_id,
            doc_id=chunk_id,
        )
    except Exception as exc:
        print(f"  [warn] dep_path extract failed for {chunk_id}: {exc}")
        triples = []

    for t in triples:
        records.append({
            "chunk_id": chunk_id,
            "subject_start": t.subject_start,
            "subject_end": t.subject_end,
            "subject_text": t.subject_surface,
            "object_start": t.object_start,
            "object_end": t.object_end,
            "object_text": t.object_surface,
            "canonical_predicate": t.predicate,
            "surface_predicate": t.predicate_lemma,
            "pattern_id": t.dep_signature[:48] if t.dep_signature else "DEP_PATH",
            "confidence": t.confidence,
            "negated": t.polarity == "NEGATIVE",
            "source": "dep_path",
        })

    # --- Source 2: SVO candidates ---
    try:
        doc = extractor._nlp(text)
        svo_list = svo_candidates(doc)
    except Exception as exc:
        print(f"  [warn] SVO extract failed for {chunk_id}: {exc}")
        svo_list = []

    # Build a span index for mapping SVO token heads to entity mentions
    span_index: list[tuple[int, int, str, str]] = [
        (es.start_char, es.end_char, es.surface, es.entity_type)
        for es in all_mentions
    ]

    def _find_entity_for_token(tok) -> tuple[int, int, str] | None:
        """Find the entity mention whose span overlaps with the token's subtree.

        Uses subtree overlap so that "fields" (head of "CAPTCHA fields") maps
        to entity "CAPTCHA" — the compound child is in the subtree.
        """
        subtree = list(tok.subtree)
        if subtree:
            tok_start = subtree[0].idx
            tok_end = subtree[-1].idx + len(subtree[-1].text)
        else:
            tok_start = tok.idx
            tok_end = tok.idx + len(tok.text)

        for s, e, surface, etype in span_index:
            # Overlap check: entity and subtree share at least 1 char
            if s < tok_end and tok_start < e:
                return (s, e, surface)
        return None

    for svo in svo_list:
        subj_ent = _find_entity_for_token(svo.subject)
        obj_ent = _find_entity_for_token(svo.object)
        if subj_ent is None or obj_ent is None:
            continue
        if subj_ent[0] == obj_ent[0]:
            continue

        verb_lemma = svo.verb.lemma_.lower()

        # Resolve through the same predicate resolver used by dep_path
        sig = "nsubjpass-VERB-dobj" if svo.passive else "nsubj-VERB-dobj"
        resolved = resolve_predicate(
            signature=sig,
            lemma=verb_lemma,
            subject_type="",
            object_type="",
        )
        if resolved is None:
            # Try flat T3 synonym lookup directly
            from services.extraction.dep_path_extractor import _load_synonyms
            syns = _load_synonyms()
            canonical = syns.get(verb_lemma)
            if canonical is None:
                continue
        else:
            canonical = resolved[0]

        records.append({
            "chunk_id": chunk_id,
            "subject_start": subj_ent[0],
            "subject_end": subj_ent[1],
            "subject_text": subj_ent[2],
            "object_start": obj_ent[0],
            "object_end": obj_ent[1],
            "object_text": obj_ent[2],
            "canonical_predicate": canonical,
            "surface_predicate": verb_lemma,
            "pattern_id": "NATIVE_SVO",
            "confidence": 1.0,
            "negated": False,
            "source": "svo",
        })

    return records


# ---------------------------------------------------------------------------
# Gold relation → Relex pair matching
# ---------------------------------------------------------------------------


def find_relex_pair_for_gold(
    gold_rel: dict,
    evidence_list: list[RelationEvidence],
) -> RelationEvidence | None:
    """Find the Relex evidence pair matching gold entity spans.

    Tries exact span match first, then morphological variant match,
    then surface-form match (same entity, different occurrence).
    """
    g_head_start = gold_rel["head_start"]
    g_head_end = gold_rel["head_end"]
    g_tail_start = gold_rel["tail_start"]
    g_tail_end = gold_rel["tail_end"]
    g_head_text = gold_rel.get("head_text", "")
    g_tail_text = gold_rel.get("tail_text", "")

    # Level 1: exact span match
    for ev in evidence_list:
        if (ev.subject_start == g_head_start and ev.subject_end == g_head_end
                and ev.object_start == g_tail_start and ev.object_end == g_tail_end):
            return ev

    # Level 2: morphological variant (spans within 2 chars)
    for ev in evidence_list:
        h_match = (
            abs(ev.subject_start - g_head_start) <= 2
            and abs(ev.subject_end - g_head_end) <= 2
        )
        t_match = (
            abs(ev.object_start - g_tail_start) <= 2
            and abs(ev.object_end - g_tail_end) <= 2
        )
        if h_match and t_match:
            if (is_morphological_variant(ev.subject_text, g_head_text)
                    or is_morphological_variant(ev.object_text, g_tail_text)):
                return ev

    # Level 3: surface-form match (same entity, different mention position)
    g_head_norm = normalized_mention_cached(g_head_text)
    g_tail_norm = normalized_mention_cached(g_tail_text)
    if g_head_norm and g_tail_norm:
        for ev in evidence_list:
            if (normalized_mention_cached(ev.subject_text) == g_head_norm
                    and normalized_mention_cached(ev.object_text) == g_tail_norm):
                return ev

    return None


# ---------------------------------------------------------------------------
# Determinism hash
# ---------------------------------------------------------------------------


def _decision_hash(decision) -> str:
    """Stable hash of a GateDecision for determinism verification."""
    payload = json.dumps({
        "status": decision.status.value,
        "predicate": decision.predicate,
        "score": round(decision.score, 6),
        "margin": round(decision.margin, 6),
        "direction_margin": round(decision.direction_margin, 6),
        "reasons": list(decision.reasons),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    gold_samples = {s["sample_id"]: s for s in load_jsonl(GOLD_PATH)}
    predictions = {p["sample_id"]: p for p in load_jsonl(PRED_PATH)}

    print("Loading policy and extractor...")
    policy = load_policy()
    extractor = DepPathExtractor()

    # Collect all gold relations with their sample
    gold_relations = []
    for sid, sample in gold_samples.items():
        entity_map = {e["entity_id"]: e for e in sample.get("entities", [])}
        for rel in sample.get("relations", []):
            head_ent = entity_map.get(rel["head_id"], {})
            tail_ent = entity_map.get(rel["tail_id"], {})
            gold_relations.append({
                **rel,
                "sample_id": sid,
                "head_start": head_ent.get("start", -1),
                "head_end": head_ent.get("end", -1),
                "head_text": head_ent.get("text", ""),
                "head_type": head_ent.get("type", ""),
                "tail_start": tail_ent.get("start", -1),
                "tail_end": tail_ent.get("end", -1),
                "tail_text": tail_ent.get("text", ""),
                "tail_type": tail_ent.get("type", ""),
            })

    print(f"\nGold relations: {len(gold_relations)}")
    print(f"{'=' * 120}")

    decisions: list[dict] = []
    total_syntax_records = 0
    run_hashes: list[str] = []
    import time as _time
    extraction_times: list[float] = []
    gate_times: list[float] = []
    relex_build_times: list[float] = []
    join_modes_seen: dict[str, int] = {}
    unknown_type_count: int = 0

    for gold_rel in gold_relations:
        sid = gold_rel["sample_id"]
        sample = gold_samples[sid]
        pred = predictions.get(sid)

        if pred is None:
            print(f"\n[SKIP] {sid}: no predictions")
            continue

        # Build Relex evidence from raw pair scores
        _t0 = _time.perf_counter()
        evidence_list = build_relation_evidence(sid, pred)
        _relex_ms = (_time.perf_counter() - _t0) * 1000
        relex_build_times.append(_relex_ms)

        # Track unknown-type count
        for ev in evidence_list:
            if ev.subject_type == "unknown" or ev.object_type == "unknown":
                unknown_type_count += 1
                break

        # Generate syntax evidence from ALL mentions of gold entity surfaces
        _t0 = _time.perf_counter()
        syntax_records = generate_syntax_for_chunk(
            sample["text"],
            sample.get("entities", []),
            sid,
            extractor,
        )
        _syntax_ms = (_time.perf_counter() - _t0) * 1000
        extraction_times.append(_syntax_ms)
        total_syntax_records += len(syntax_records)

        # Join syntax to Relex evidence (with surface-form join enabled)
        joined = join_syntax_evidence(evidence_list, syntax_records, surface_join=True)

        # Find the pair matching this gold relation
        ev = find_relex_pair_for_gold(gold_rel, joined)

        head_text = gold_rel.get("head_text", "")
        tail_text = gold_rel.get("tail_text", "")
        gold_pred = gold_rel["predicate"]

        if ev is None:
            print(f"\n[PAIR_NOT_FOUND] {head_text} → {gold_pred} → {tail_text}")
            print(f"  (sample={sid}, head_span={gold_rel['head_start']},{gold_rel['head_end']})")
            decisions.append({
                "sid": sid, "gold_pred": gold_pred,
                "triple": f"{head_text}→{gold_pred}→{tail_text}",
                "status": "PAIR_NOT_FOUND",
                "gate_pred": None, "pred_match": False,
                "score": 0.0, "has_syntax": False,
                "syntax_preds": [], "disagreement": False,
            })
            # Deterministic hash for pair-not-found so the master hash
            # covers all 18 gold relations.
            not_found_payload = json.dumps({
                "status": "PAIR_NOT_FOUND",
                "sid": sid,
                "gold_pred": gold_pred,
                "triple": f"{head_text}→{gold_pred}→{tail_text}",
            }, sort_keys=True)
            run_hashes.append(
                hashlib.sha256(not_found_payload.encode()).hexdigest()[:16]
            )
            continue

        # Run the gate
        _t0 = _time.perf_counter()
        decision = evaluate_relation(ev, policy)
        _gate_ms = (_time.perf_counter() - _t0) * 1000
        gate_times.append(_gate_ms)
        run_hashes.append(_decision_hash(decision))

        # Track join modes on this evidence
        for syn in ev.syntax_evidence:
            join_modes_seen[syn.join_mode] = (
                join_modes_seen.get(syn.join_mode, 0) + 1
            )

        # Compute diagnostics
        top_score = max((ps.score for ps in ev.predicate_scores), default=0.0)
        has_syntax = len(ev.syntax_evidence) > 0
        syntax_preds = [s.canonical_predicate for s in ev.syntax_evidence]

        # Check if the gate's predicate matches the gold predicate
        pred_match = decision.predicate == gold_pred if decision.predicate else False

        # Detect syntax/Relex disagreement
        disagreement = has_syntax and decision.predicate not in syntax_preds

        status_marker = "✓" if pred_match and decision.status in (
            GateStatus.ACCEPT_HIGH, GateStatus.ACCEPT_CORROBORATED
        ) else ("~" if disagreement else "✗")

        print(f"\n[{status_marker} {decision.status.value}] {head_text} → {gold_pred} → {tail_text}")
        print(f"  gold_pred={gold_pred}  sigmoid_score={top_score:.4f}  sigmoid_margin={decision.margin:.4f}  sigmoid_dir_margin={decision.direction_margin:.4f}")
        print(f"  gate_pred={decision.predicate}  pred_match={pred_match}")
        print(f"  logit_margin={decision.logit_margin:.2f}  logit_dir_margin={decision.logit_direction_margin:.2f}")
        print(f"  syntax={'yes' if has_syntax else 'no'}({syntax_preds})")
        print(f"  reasons={list(decision.reasons)}")

        decisions.append({
            "sid": sid, "gold_pred": gold_pred,
            "triple": f"{head_text}→{gold_pred}→{tail_text}",
            "status": decision.status.value,
            "gate_pred": decision.predicate, "pred_match": pred_match,
            "score": top_score, "has_syntax": has_syntax,
            "syntax_preds": syntax_preds, "disagreement": disagreement,
        })

    # ======================================================================
    # SUMMARY
    # ======================================================================
    print(f"\n{'=' * 120}")
    print("SUMMARY")
    print(f"{'=' * 120}")

    n = len(decisions)

    # Count by status
    status_counts: dict[str, int] = {}
    for d in decisions:
        status_counts[d["status"]] = status_counts.get(d["status"], 0) + 1

    print("\nGate status distribution:")
    for status, count in sorted(status_counts.items()):
        print(f"  {status}: {count}")

    # Core metrics
    accept_high = status_counts.get("accept_high", 0)
    accept_corr = status_counts.get("accept_corroborated", 0)
    review = status_counts.get("review_conflict", 0)
    pair_not_found = status_counts.get("PAIR_NOT_FOUND", 0)

    # Precision: accepted with CORRECT predicate / total accepted
    total_accepted = accept_high + accept_corr
    correct_accepts = sum(
        1 for d in decisions
        if d["status"] in ("accept_high", "accept_corroborated")
        and d["pred_match"]
    )
    false_positives = total_accepted - correct_accepts

    # Recall: correct accepts / total gold
    recall = correct_accepts / n if n > 0 else 0.0
    precision = correct_accepts / total_accepted if total_accepted > 0 else 0.0

    # ACCEPT_HIGH precision/recall
    accept_high_correct = sum(
        1 for d in decisions
        if d["status"] == "accept_high" and d["pred_match"]
    )
    accept_high_precision = (
        accept_high_correct / accept_high if accept_high > 0 else 0.0
    )
    accept_high_recall = accept_high_correct / n if n > 0 else 0.0

    # ACCEPT_CORROBORATED precision/recall
    accept_corr_correct = sum(
        1 for d in decisions
        if d["status"] == "accept_corroborated" and d["pred_match"]
    )
    accept_corr_precision = (
        accept_corr_correct / accept_corr if accept_corr > 0 else 0.0
    )
    accept_corr_recall = accept_corr_correct / n if n > 0 else 0.0

    # Syntax metrics
    syntax_joined_count = sum(1 for d in decisions if d["has_syntax"])
    disagreement_count = sum(1 for d in decisions if d["disagreement"])
    unjoined_rate = 1.0 - (syntax_joined_count / n) if n > 0 else 0.0
    avg_syntax_ms = sum(extraction_times) / len(extraction_times) if extraction_times else 0.0
    avg_relex_ms = sum(relex_build_times) / len(relex_build_times) if relex_build_times else 0.0
    avg_gate_ms = sum(gate_times) / len(gate_times) if gate_times else 0.0
    avg_total_ms = avg_relex_ms + avg_syntax_ms + avg_gate_ms

    # Determinism: all hashes should be stable (we can't verify cross-run here,
    # but we report the hash for manual verification)
    all_hashes_match = len(set(run_hashes)) == len(run_hashes) if run_hashes else True

    print(f"\n--- Verification Checkpoints ---")
    print(f"  Total gold relations evaluated:     {n}")
    print(f"  ACCEPT_HIGH:                        {accept_high}")
    print(f"  ACCEPT_CORROBORATED:                {accept_corr}")
    print(f"  REVIEW_CONFLICT:                    {review}")
    print(f"  PAIR_NOT_FOUND:                     {pair_not_found}")
    print(f"  Total accepted (HIGH + CORR):       {total_accepted}/{n}")
    print(f"  Baseline (HIT at thr=0.30):         7/18")
    print("")
    print(f"  Combined precision (correct pred):  {precision:.3f} ({correct_accepts}/{total_accepted})")
    print(f"  Combined recall:                    {recall:.3f} ({correct_accepts}/{n})")
    print(f"  False positives (wrong pred):       {false_positives}")
    print(f"  ACCEPT_HIGH precision:              {accept_high_precision:.3f} ({accept_high_correct}/{accept_high})")
    print(f"  ACCEPT_HIGH recall:                 {accept_high_recall:.3f} ({accept_high_correct}/{n})")
    print(f"  ACCEPT_CORROBORATED precision:      {accept_corr_precision:.3f} ({accept_corr_correct}/{accept_corr})")
    print(f"  ACCEPT_CORROBORATED recall:         {accept_corr_recall:.3f} ({accept_corr_correct}/{n})")
    print("")
    print(f"  Syntax records generated:           {total_syntax_records}")
    print(f"  Gold relations with syntax joined:  {syntax_joined_count}/{n}")
    print(f"  Unjoined evidence rate:             {unjoined_rate:.3f}")
    print(f"  Syntax/Relex disagreement count:    {disagreement_count}")
    print(f"  Syntax/Relex disagreement rate:     {disagreement_count}/{syntax_joined_count} ({disagreement_count/max(syntax_joined_count,1):.3f})")
    print(f"  Unknown-type relation count:        {unknown_type_count}")
    print("")
    print(f"--- Join-Mode Distribution ---")
    for mode in ("exact_span", "canonical_entity", "resolved_alias", "normalized_surface"):
        print(f"  {mode:24s} {join_modes_seen.get(mode, 0)}")
    print("")
    print(f"--- Latency ---")
    print(f"  Avg Relex evidence build:           {avg_relex_ms:.2f} ms/sample")
    print(f"  Avg syntax extraction:              {avg_syntax_ms:.1f} ms/chunk")
    print(f"  Avg gate evaluation:                {avg_gate_ms:.3f} ms/pair")
    print(f"  Avg total per sample:               {avg_total_ms:.1f} ms")
    print(f"  Determinism (unique hashes):        {len(set(run_hashes))} unique / {len(run_hashes)} total")
    # Master hash: concatenation of all per-pair hashes, hashed again.
    # Two runs MUST produce the same master hash for deterministic output.
    master_hash = hashlib.sha256(
        "|".join(run_hashes).encode()
    ).hexdigest()[:32]
    print(f"  MASTER RUN HASH (compare across runs): {master_hash}")

    # List specific recoveries and conflicts
    print(f"\n--- ACCEPT_CORROBORATED Relations ---")
    for d in decisions:
        if d["status"] == "accept_corroborated":
            print(f"  {d['triple']}  (score={d['score']:.4f}, pred_match={d['pred_match']})")

    print(f"\n--- ACCEPT_HIGH with WRONG predicate (false positives) ---")
    for d in decisions:
        if d["status"] == "accept_high" and not d["pred_match"]:
            print(f"  {d['triple']}  gate_pred={d['gate_pred']}  score={d['score']:.4f}")

    print(f"\n--- REVIEW_CONFLICT Relations ---")
    for d in decisions:
        if d["status"] == "review_conflict":
            print(f"  {d['triple']}  (score={d['score']:.4f})")

    print(f"\n--- REJECTED Relations ---")
    for d in decisions:
        if d["status"].startswith("reject") or d["status"] == "PAIR_NOT_FOUND":
            print(f"  [{d['status']}] {d['triple']}  (score={d['score']:.4f}, syntax={'yes' if d['has_syntax'] else 'no'})")


if __name__ == "__main__":
    main()

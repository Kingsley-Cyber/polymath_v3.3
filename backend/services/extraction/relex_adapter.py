"""Adapter: converts Relex raw predictions + syntax candidates → RelationEvidence.

This module is the glue between:
  - Relex raw_pair_scores JSONL (all 28 labels × every ordered pair)
  - DependencyMatcher / SVO candidate output
  - The corroboration gate

It does NOT call the Relex model directly. It consumes the already-exported
raw pair scores from the goldscore benchmark JSONL format.

The adapter produces RelationEvidence records keyed by stable span offsets:
    (chunk_id, subject_start, subject_end, object_start, object_end)

Syntax evidence from the dep-path pipeline is joined to Relex pairs by
matching those span offsets. When model-decoded entity spans differ from
the gold/expected spans by a morphological variant (singular/plural), the
mention_normalizer bridges the gap.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from services.extraction.relation_evidence import (
    JOIN_TRUST,
    JoinMode,
    PredicateScore,
    RelationEvidence,
    SyntaxEvidence,
    TypeFailureStage,
    TypeStatus,
)
from services.extraction.mention_normalizer import (
    is_morphological_variant,
    normalized_mention_cached,
)
# Canonical predicate label mapping is owned by services.extraction.canonical.
# Re-export here for backwards-compat with callers that imported it from
# relex_adapter — but DO NOT redefine. The canonical-contract test enforces
# that canonicalization logic lives only in canonical.py.
from services.extraction.canonical import (
    canonical_entity_type,
    canonicalize_predicate_label,
    entity_id_from_name,
    normalize_entity_name,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Relex JSONL → RelationEvidence
# ---------------------------------------------------------------------------


def _pair_key(head: dict, tail: dict) -> tuple[int, int, int, int]:
    """Stable span key for one ordered pair."""
    return (
        int(head["start"]),
        int(head["end"]),
        int(tail["start"]),
        int(tail["end"]),
    )


def _build_predicate_scores(
    raw_scores: dict[str, float],
) -> list[PredicateScore]:
    """Convert model label scores → PredicateScore list, preserving ALL labels.

    Labels are canonicalized from Relex space-form to underscore-form so
    they match the ontology, dep_path resolver, and YAML config.
    """
    return [
        PredicateScore(
            predicate=canonicalize_predicate_label(label),
            score=float(score),
        )
        for label, score in sorted(raw_scores.items())
        if score is not None
    ]


def build_relation_evidence(
    chunk_id: str,
    prediction_row: dict[str, Any],
    *,
    oracle_types: dict[tuple[int, int], str] | None = None,
) -> list[RelationEvidence]:
    """Convert one Relex prediction JSONL row into RelationEvidence records.

    Each ordered pair in raw_pair_scores becomes one RelationEvidence. The
    reverse_scores for each pair are looked up from the reversed pair's
    scores in the same row.

    Entity type resolution (P1A typed-endpoint contract):
      1. head/tail ``type`` field (if the prediction format carries it)
      2. Entity-list offset join: match (start, end) to the prediction's
         ``entities`` list and use that entity's ``type`` / ``label``
      3. Oracle types (evaluation only — injected from gold)
      4. Fallback: "unknown"

    oracle_types: optional mapping of (start, end) → type string, injected
    from gold entity annotations for diagnostic replay. NEVER used in
    production. When present, overrides the unresolved "unknown" fallback.
    """
    raw_pairs = prediction_row.get("raw_pair_scores", [])
    if not raw_pairs:
        return []

    # Build offset → entity type index from the entity list.
    # GLiNER-Relex emits entities with labels; the entity list is the
    # authoritative source for endpoint types when the raw_pair_scores
    # head/tail dicts lack a ``type`` field.
    entity_type_by_offset: dict[tuple[int, int], str] = {}
    entity_score_by_offset: dict[tuple[int, int], float] = {}
    for ent in prediction_row.get("entities", []):
        e_start = int(ent.get("start", -1))
        e_end = int(ent.get("end", -1))
        # Prefer 'label' (GLiNER native) over 'type' (serialized alias).
        etype = ent.get("label") or ent.get("type") or ent.get("entity_type") or ""
        if e_start >= 0 and e_end > e_start:
            if etype:
                entity_type_by_offset[(e_start, e_end)] = str(etype)
            entity_score_by_offset[(e_start, e_end)] = float(ent.get("score", 0.0))

    # --- P1A Hard Invariant: raw_relex_type_coverage ---
    # In a correctly serialized prediction, every entity should have a label.
    # When type coverage is 0% and entities exist, the failure stage is
    # TYPE_DROPPED_DURING_SERIALIZATION.
    raw_entities = prediction_row.get("entities", [])
    _type_coverage = (
        len(entity_type_by_offset) / len(raw_entities)
        if raw_entities else 1.0
    )
    _serialization_loss = (
        len(raw_entities) > 0 and len(entity_type_by_offset) == 0
    )

    # Index pairs by span key for O(1) reverse lookup
    pair_by_key: dict[tuple[int, int, int, int], dict[str, float]] = {}
    pair_meta: dict[tuple[int, int, int, int], dict[str, Any]] = {}

    for rp in raw_pairs:
        head = rp["head"]
        tail = rp["tail"]
        key = _pair_key(head, tail)
        pair_by_key[key] = rp.get("scores", {})
        pair_meta[key] = {"head": head, "tail": tail}

    evidence_list: list[RelationEvidence] = []

    for key, scores in pair_by_key.items():
        h_start, h_end, t_start, t_end = key
        meta = pair_meta[key]
        head = meta["head"]
        tail = meta["tail"]

        # Find reverse pair for direction_margin computation.
        reverse_key = (t_start, t_end, h_start, h_end)
        reverse_raw = pair_by_key.get(reverse_key, {})
        reverse_scores: dict[str, float] = {
            canonicalize_predicate_label(k): float(v)
            for k, v in reverse_raw.items()
            if v is not None
        }

        pred_scores = _build_predicate_scores(scores)

        # --- Endpoint type resolution (P1A) ---
        # Priority: head/tail type field → entity-list offset join →
        #           oracle (eval only) → unknown.
        s_type_raw = (
            head.get("type")
            or head.get("label")
            or entity_type_by_offset.get((h_start, h_end))
            or ""
        )
        o_type_raw = (
            tail.get("type")
            or tail.get("label")
            or entity_type_by_offset.get((t_start, t_end))
            or ""
        )

        # Determine type provenance status and failure stage.
        s_score = entity_score_by_offset.get((h_start, h_end), 0.0)
        o_score = entity_score_by_offset.get((t_start, t_end), 0.0)

        # Subject type status
        if s_type_raw:
            s_type_status = TypeStatus.RELEX_ONLY.value
            s_failure = ""
        elif oracle_types and (h_start, h_end) in oracle_types:
            s_type_raw = oracle_types[(h_start, h_end)]
            s_type_status = TypeStatus.ORACLE.value
            s_failure = ""
        else:
            s_type_status = TypeStatus.UNKNOWN.value
            s_failure = (
                TypeFailureStage.TYPE_DROPPED_DURING_SERIALIZATION.value
                if _serialization_loss
                else TypeFailureStage.RAW_MODEL_TYPE_MISSING.value
            )

        # Object type status
        if o_type_raw:
            o_type_status = TypeStatus.RELEX_ONLY.value
            o_failure = ""
        elif oracle_types and (t_start, t_end) in oracle_types:
            o_type_raw = oracle_types[(t_start, t_end)]
            o_type_status = TypeStatus.ORACLE.value
            o_failure = ""
        else:
            o_type_status = TypeStatus.UNKNOWN.value
            o_failure = (
                TypeFailureStage.TYPE_DROPPED_DURING_SERIALIZATION.value
                if _serialization_loss
                else TypeFailureStage.RAW_MODEL_TYPE_MISSING.value
            )

        # Use the worst failure stage for the record-level diagnostic.
        failure_stage = s_failure or o_failure

        evidence = RelationEvidence(
            chunk_id=chunk_id,
            subject_id=f"span:{h_start}:{h_end}",
            subject_text=head.get("text", ""),
            subject_type=canonical_entity_type(s_type_raw),
            subject_start=h_start,
            subject_end=h_end,
            object_id=f"span:{t_start}:{t_end}",
            object_text=tail.get("text", ""),
            object_type=canonical_entity_type(o_type_raw),
            object_start=t_start,
            object_end=t_end,
            predicate_scores=tuple(pred_scores),
            reverse_scores=dict(reverse_scores),
            syntax_evidence=(),  # filled by join_syntax_evidence
            raw_relex_type=s_type_raw or o_type_raw,
            raw_relex_type_score=max(s_score, o_score),
            effective_type_status=s_type_status,
            object_type_status=o_type_status,
            type_failure_stage=failure_stage,
        )
        evidence_list.append(evidence)

    return evidence_list


# ---------------------------------------------------------------------------
# Syntax evidence joining
# ---------------------------------------------------------------------------


def join_syntax_evidence(
    evidence_list: list[RelationEvidence],
    syntax_records: Iterable[dict[str, Any]],
    *,
    surface_join: bool = False,
    enable_head_token: bool = True,
) -> list[RelationEvidence]:
    """Attach SyntaxEvidence to matching RelationEvidence records by span.

    syntax_records is an iterable of dicts with keys:
        chunk_id, subject_start, subject_end, object_start, object_end,
        canonical_predicate, surface_predicate, pattern_id, confidence,
        negated (optional), modal (optional)
        subject_text, object_text (optional — needed for surface_join)
        source_family (optional — defaults to spacy_dependency_parse)

    Join strategy (in priority order). Each level tags the resulting
    SyntaxEvidence with a join_mode describing how the match was made:
      1. EXACT_SPAN (trust 1.00): exact match on
         (chunk_id, subj_start, subj_end, obj_start, obj_end).
      2. CANONICAL_ENTITY (trust 0.80): morphological variant — spans
         within 2 chars on both endpoints. Same canonical entity at a
         slightly different mention position.
      3. RESOLVED_ALIAS (trust 0.70): the alias map resolves one entity's
         name onto the other's canonical form. Same entity via aliasing.
      4. NORMALIZED_SURFACE (trust 0.30, shadow-only): matches by
         normalized surface text when syntax records carry subject_text /
         object_text. Used in shadow mode where gold entity spans differ
         from the mention where the relation is syntactically expressed.
         CANNOT trigger REVIEW_CONFLICT or authorize a production write.
    """
    # Build lookup: chunk_id → {span_key → [(SyntaxEvidence, record_dict)]}
    syntax_by_chunk: dict[str, dict[tuple, list[tuple[SyntaxEvidence, dict]]]] = {}
    for rec in syntax_records:
        cid = rec.get("chunk_id", "")
        key = (
            int(rec.get("subject_start", -1)),
            int(rec.get("subject_end", -1)),
            int(rec.get("object_start", -1)),
            int(rec.get("object_end", -1)),
        )
        se = SyntaxEvidence(
            canonical_predicate=str(rec.get("canonical_predicate") or ""),
            surface_predicate=str(rec.get("surface_predicate") or ""),
            pattern_id=str(rec.get("pattern_id", "UNKNOWN")),
            confidence=float(rec.get("confidence", 1.0)),
            negated=bool(rec.get("negated", False)),
            modal=bool(rec.get("modal", False)),
            source_family=str(rec.get("source_family", "spacy_dependency_parse")),
            # join_mode is set per-match below — default is exact_span.
            # Argument-role and direction provenance (open-relation routing)
            subject_dependency_role=str(rec.get("subject_dependency_role", "")),
            object_dependency_role=str(rec.get("object_dependency_role", "")),
            voice=str(rec.get("voice", "")),
            direction_source=str(rec.get("direction_source", "")),
            direction_confidence=str(rec.get("direction_confidence", "")),
        )
        syntax_by_chunk.setdefault(cid, {}).setdefault(key, []).append((se, rec))

    updated: list[RelationEvidence] = []
    for ev in evidence_list:
        chunk_syntax = syntax_by_chunk.get(ev.chunk_id, {})
        exact_key = (ev.subject_start, ev.subject_end, ev.object_start, ev.object_end)

        # Track (syntax_evidence, join_mode) so we can rebuild SyntaxEvidence
        # with the correct join_mode tagged.
        matched: list[tuple[SyntaxEvidence, str]] = []

        # Level 1: exact span match → EXACT_SPAN
        for se, rec in chunk_syntax.get(exact_key, []):
            matched.append((se, JoinMode.EXACT_SPAN.value))

        # Level 2: morphological variant → CANONICAL_ENTITY
        if not matched:
            for syn_key, syn_list in chunk_syntax.items():
                if _spans_are_morphological_variants(exact_key, syn_key):
                    for se, rec in syn_list:
                        matched.append((se, JoinMode.CANONICAL_ENTITY.value))

        # Level 3: alias resolution → RESOLVED_ALIAS
        # Fires when canonical IDs match via alias resolution but the
        # surface forms differ — i.e. the alias map joined two mentions
        # of the same canonical entity that wouldn't otherwise match.
        if not matched:
            ev_subj_canon = entity_id_from_name(ev.subject_text)
            ev_obj_canon = entity_id_from_name(ev.object_text)
            ev_subj_norm = normalize_entity_name(ev.subject_text)
            ev_obj_norm = normalize_entity_name(ev.object_text)
            for syn_key, syn_list in chunk_syntax.items():
                for se, rec in syn_list:
                    rec_subj_text = rec.get("subject_text", "")
                    rec_obj_text = rec.get("object_text", "")
                    if not rec_subj_text or not rec_obj_text:
                        continue
                    rec_subj_canon = entity_id_from_name(rec_subj_text)
                    rec_obj_canon = entity_id_from_name(rec_obj_text)
                    rec_subj_norm = normalize_entity_name(rec_subj_text)
                    rec_obj_norm = normalize_entity_name(rec_obj_text)
                    # Same canonical IDs (alias resolution aligned them)
                    # AND surface forms differ (it wasn't an exact match).
                    if (
                        ev_subj_canon == rec_subj_canon
                        and ev_obj_canon == rec_obj_canon
                        and (
                            ev_subj_norm != rec_subj_norm
                            or ev_obj_norm != rec_obj_norm
                        )
                    ):
                        matched.append((se, JoinMode.RESOLVED_ALIAS.value))

        # Level 4: true head-token containment → HEAD_TOKEN (P2A)
        # Fires when the syntax argument's actual spaCy head token lies
        # inside the Relex entity mention's character span. This is the
        # genuine structural check: the head token (e.g. "dog" in "the big
        # dog") must be contained within [entity_start, entity_end).
        #
        # Invariants:
        #   - Uses the head token's character offsets, NOT the full phrase span
        #   - entity_start <= head_start and head_end <= entity_end
        #   - Never crosses sentence boundaries (inherent: same chunk)
        #   - Subject and object cannot resolve to the same mention
        #   - Trust 0.60: above span containment, below alias resolution
        if not matched and enable_head_token:
            for syn_key, syn_list in chunk_syntax.items():
                for se, rec in syn_list:
                    # Head-token offsets from the FrameExtractor parse
                    s_head_start = int(rec.get("subject_head_start", -1))
                    s_head_end = int(rec.get("subject_head_end", -1))
                    o_head_start = int(rec.get("object_head_start", -1))
                    o_head_end = int(rec.get("object_head_end", -1))
                    if s_head_start < 0 or o_head_start < 0:
                        continue  # no head-token info available

                    # True head-token containment: head token inside entity span
                    subj_head_in = (
                        ev.subject_start <= s_head_start
                        and s_head_end <= ev.subject_end
                    )
                    obj_head_in = (
                        ev.object_start <= o_head_start
                        and o_head_end <= ev.object_end
                    )
                    # At least one endpoint's head must be contained;
                    # the other must be exact or also contained.
                    syn_s_start, syn_s_end, syn_o_start, syn_o_end = syn_key
                    subj_exact = (ev.subject_start == syn_s_start
                                  and ev.subject_end == syn_s_end)
                    obj_exact = (ev.object_start == syn_o_start
                                 and ev.object_end == syn_o_end)
                    if (
                        (subj_head_in and (obj_head_in or obj_exact))
                        or (obj_head_in and (subj_head_in or subj_exact))
                    ):
                        # Guard: subject and object must not resolve to the
                        # same mention (same span on both endpoints).
                        if (ev.subject_start == ev.object_start
                                and ev.subject_end == ev.object_end):
                            continue
                        matched.append((se, JoinMode.HEAD_TOKEN.value))

        # Level 4b: phrase-span containment → SPAN_CONTAINMENT (P2A)
        # Fires when the syntax argument's full character span is properly
        # contained within the entity mention's span (or vice versa).
        # Weaker than head-token: the phrase may overlap without the head
        # being inside. Trust 0.50.
        if not matched and enable_head_token:
            for syn_key, syn_list in chunk_syntax.items():
                syn_s_start, syn_s_end, syn_o_start, syn_o_end = syn_key
                for se, rec in syn_list:
                    subj_contained = _span_contains(
                        ev.subject_start, ev.subject_end,
                        syn_s_start, syn_s_end,
                    )
                    obj_contained = _span_contains(
                        ev.object_start, ev.object_end,
                        syn_o_start, syn_o_end,
                    )
                    subj_exact = (ev.subject_start == syn_s_start
                                  and ev.subject_end == syn_s_end)
                    obj_exact = (ev.object_start == syn_o_start
                                 and ev.object_end == syn_o_end)
                    if (
                        (subj_contained and (obj_contained or obj_exact))
                        or (obj_contained and (subj_contained or subj_exact))
                    ):
                        if (ev.subject_start == ev.object_start
                                and ev.subject_end == ev.object_end):
                            continue
                        matched.append((se, JoinMode.SPAN_CONTAINMENT.value))

        # Level 5: surface-form join → NORMALIZED_SURFACE (shadow-only)
        if not matched and surface_join:
            ev_subj_norm = normalized_mention_cached(ev.subject_text)
            ev_obj_norm = normalized_mention_cached(ev.object_text)
            if ev_subj_norm and ev_obj_norm:
                for syn_key, syn_list in chunk_syntax.items():
                    for se, rec in syn_list:
                        rec_subj = normalized_mention_cached(
                            rec.get("subject_text", "")
                        )
                        rec_obj = normalized_mention_cached(
                            rec.get("object_text", "")
                        )
                        if rec_subj == ev_subj_norm and rec_obj == ev_obj_norm:
                            matched.append((se, JoinMode.NORMALIZED_SURFACE.value))

        # Rebuild SyntaxEvidence records with the correct join_mode tagged.
        # frozen dataclass → must construct fresh instances.
        syntax_ev = [
            SyntaxEvidence(
                canonical_predicate=se.canonical_predicate,
                surface_predicate=se.surface_predicate,
                pattern_id=se.pattern_id,
                confidence=se.confidence,
                negated=se.negated,
                modal=se.modal,
                source_family=se.source_family,
                join_mode=mode,
                # Preserve argument-role and direction provenance
                subject_dependency_role=se.subject_dependency_role,
                object_dependency_role=se.object_dependency_role,
                voice=se.voice,
                direction_source=se.direction_source,
                direction_confidence=se.direction_confidence,
            )
            for se, mode in matched
        ]

        updated.append(
            RelationEvidence(
                chunk_id=ev.chunk_id,
                subject_id=ev.subject_id,
                subject_text=ev.subject_text,
                subject_type=ev.subject_type,
                subject_start=ev.subject_start,
                subject_end=ev.subject_end,
                object_id=ev.object_id,
                object_text=ev.object_text,
                object_type=ev.object_type,
                object_start=ev.object_start,
                object_end=ev.object_end,
                predicate_scores=ev.predicate_scores,
                reverse_scores=ev.reverse_scores,
                syntax_evidence=tuple(syntax_ev),
                same_sentence=ev.same_sentence,
                raw_relex_type=ev.raw_relex_type,
                raw_relex_type_score=ev.raw_relex_type_score,
                effective_type_status=ev.effective_type_status,
                object_type_status=ev.object_type_status,
                type_failure_stage=ev.type_failure_stage,
            )
        )
    return updated


def _span_contains(
    outer_start: int, outer_end: int,
    inner_start: int, inner_end: int,
) -> bool:
    """True when one span properly contains the other (P2A HEAD_TOKEN join).

    Returns True iff:
      - [inner_start, inner_end) is strictly inside [outer_start, outer_end)
      - OR [outer_start, outer_end) is strictly inside [inner_start, inner_end)
      - AND the spans are NOT identical (identical = Level 1 EXACT_SPAN)

    This is a structural check using character offsets from the same parsed
    Doc. It does NOT use lexical equality, substring matching, or shared
    words. The syntax argument's actual token span must be contained inside
    the candidate entity's token span (or vice versa).

    Examples (character offsets):
      entity=[0,17] syntax=[14,19]  → True  ("A CHRISTMAS STORY" contains "story")
      entity=[0,5]  syntax=[0,5]    → False (identical — handled by EXACT_SPAN)
      entity=[0,5]  syntax=[10,15]  → False (disjoint)
      entity=[0,5]  syntax=[3,8]    → False (partial overlap, not containment)
    """
    # Identical spans are not containment — they're exact matches (Level 1).
    if outer_start == inner_start and outer_end == inner_end:
        return False
    # outer contains inner
    if outer_start <= inner_start and inner_end <= outer_end:
        return True
    # inner contains outer
    if inner_start <= outer_start and outer_end <= inner_end:
        return True
    return False


def _spans_are_morphological_variants(
    key_a: tuple[int, int, int, int],
    key_b: tuple[int, int, int, int],
) -> bool:
    """Check if two span keys might be singular/plural variants of each other.

    This is a heuristic: if the spans overlap heavily (differ by 1-2 chars on
    either end) they MIGHT be morphological variants. The actual check is
    done at join time against entity text. For now, just check span proximity.
    """
    h_start_a, h_end_a, t_start_a, t_end_a = key_a
    h_start_b, h_end_b, t_start_b, t_end_b = key_b

    # Both head and tail must be within 2 chars of each other
    head_close = abs(h_start_a - h_start_b) <= 2 and abs(h_end_a - h_end_b) <= 2
    tail_close = abs(t_start_a - t_start_b) <= 2 and abs(t_end_a - t_end_b) <= 2
    return head_close and tail_close and key_a != key_b


# ---------------------------------------------------------------------------
# SVO → SyntaxEvidence conversion
# ---------------------------------------------------------------------------


def svo_to_syntax_records(
    svo_candidates: Iterable[Any],
    chunk_id: str,
    entity_spans: Sequence[dict[str, Any]],
    resolve_lemma: Any = None,
) -> list[dict[str, Any]]:
    """Convert native SVO candidates into syntax record dicts.

    svo_candidates: iterable of SVOCandidate (from svo_candidates.py) or
        dicts with subject, verb, object fields.

    entity_spans: the entity list for this chunk (dicts with start, end, text,
        type) used to map SVO token positions to entity span offsets.

    resolve_lemma: optional callable(lemma) → (canonical_predicate, swap)
        or None. When None, the raw verb lemma is used as the canonical
        predicate (the gate handles unknown predicates via defaults).
    """
    records: list[dict[str, Any]] = []

    # Build token-position → entity-span mapping
    span_map: list[tuple[int, int, int, int, str, str]] = []
    for ent in entity_spans:
        s = int(ent.get("start", ent.get("start_char", -1)))
        e = int(ent.get("end", ent.get("end_char", -1)))
        text = str(ent.get("text", ent.get("surface_form", "")))
        etype = str(ent.get("type", ent.get("entity_type", "")))
        span_map.append((s, e, s, e, text, etype))

    for svo in svo_candidates:
        subj_tok = getattr(svo, "subject", None) or svo.get("subject")
        verb_tok = getattr(svo, "verb", None) or svo.get("verb")
        obj_tok = getattr(svo, "object", None) or svo.get("object")

        if subj_tok is None or verb_tok is None or obj_tok is None:
            continue

        # Map token head indices to entity spans
        subj_span = _find_entity_for_token(subj_tok, entity_spans)
        obj_span = _find_entity_for_token(obj_tok, entity_spans)
        if subj_span is None or obj_span is None:
            continue

        verb_lemma = getattr(verb_tok, "lemma_", None) or verb_tok.get("lemma_", "")

        canonical_pred = verb_lemma.lower()
        if resolve_lemma is not None:
            resolved = resolve_lemma(verb_lemma.lower())
            if resolved is not None:
                pred, _swap = resolved if isinstance(resolved, tuple) else (resolved, False)
                canonical_pred = pred

        records.append({
            "chunk_id": chunk_id,
            "subject_start": subj_span[0],
            "subject_end": subj_span[1],
            "object_start": obj_span[0],
            "object_end": obj_span[1],
            "canonical_predicate": canonical_pred,
            "surface_predicate": verb_lemma,
            "pattern_id": "NATIVE_SVO",
            "confidence": 1.0,
            "negated": False,
            "modal": False,
        })

    return records


def _find_entity_for_token(
    token: Any,
    entity_spans: Sequence[dict[str, Any]],
) -> tuple[int, int] | None:
    """Find the entity span whose head token matches the given token."""
    tok_idx = getattr(token, "i", None)
    if tok_idx is None and isinstance(token, dict):
        tok_idx = token.get("i")
    if tok_idx is None:
        return None

    for ent in entity_spans:
        start = int(ent.get("start", ent.get("start_char", -1)))
        end = int(ent.get("end", ent.get("end_char", -1)))
        # If the token falls within this entity's span, return it
        # This works when token offsets are character offsets (from Doc)
        # The svo_candidates yield Token objects, so we check token.idx_
        tok_start = getattr(token, "idx", None)
        if tok_start is not None and start <= tok_start < end:
            return (start, end)
    return None


# ---------------------------------------------------------------------------
# Batch loading from JSONL
# ---------------------------------------------------------------------------


def load_predictions_jsonl(path: str | Path) -> dict[str, dict[str, Any]]:
    """Load a Relex predictions JSONL file keyed by sample_id.

    Each line is one prediction row with sample_id, entities, raw_pair_scores.
    """
    result: dict[str, dict[str, Any]] = {}
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            row = json.loads(line)
            sid = row.get("sample_id", "")
            if sid:
                result[sid] = row
    return result

"""Production syntax lane for the relex_local extraction engine.

One-spaCy-parse production copy of the unified shadow pipeline's syntax
generation (backend/scripts/unified_shadow_pipeline.py). Differences from
the benchmark script are deliberate and structural:

  * PARSE ONCE — a single ``spacy.tokens.Doc`` is parsed by the caller and
    passed into both ``FrameExtractor.extract(doc=...)`` and
    ``svo_candidates(doc)``. The shadow script parsed twice; the production
    invariant is exactly one spaCy parse per chunk.
  * NO ORACLE TYPES — ``build_union_evidence`` never accepts gold-injected
    endpoint types. Endpoint types come exclusively from the Relex
    prediction row.
  * FIXED PRODUCTION FEATURES — no ablation switches at runtime; the lane
    always runs the full ``PRODUCTION_FEATURES`` profile so re-runs of the
    same chunk are deterministic.

Lane steps per chunk:
  1. Expand Relex entity spans to ALL surface mentions
  2. FrameExtractor frames on the shared parse
  3. Native SVO on the same parse
  4. Credit/metadata token-pattern lane
  5. Union with Relex-scored pairs (relex_adapter joins)
  6. Syntax-only evidence for frames with no Relex counterpart
  7. Same-sentence / cross-sentence scope annotation
"""

from __future__ import annotations

import re
from dataclasses import replace

from services.extraction.ablation import PRODUCTION_FEATURES
from services.extraction.dep_path_extractor import EntitySpan, resolve_predicate
from services.extraction.frame_extractor import FrameExtractor
from services.extraction.mention_normalizer import normalized_mention_cached
from services.extraction.relation_evidence import (
    RelationEvidence,
    SyntaxEvidence,
)
from services.extraction.relex_adapter import (
    build_relation_evidence,
    join_syntax_evidence,
)
from services.extraction.svo_candidates import svo_candidates

# ---------------------------------------------------------------------------
# Entity mention expansion: find ALL occurrences of each Relex entity surface
# ---------------------------------------------------------------------------

_BOUNDARY_CHARS = frozenset(" \n\t\r.,;:!?()[]{}\"'\u201c\u201d\u2018\u2019*\u2014-_/~")


def _find_all_mentions(text: str, surface: str) -> list[tuple[int, int]]:
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
        before = text[idx - 1] if idx > 0 else " "
        after_pos = idx + slen
        after = text[after_pos] if after_pos < len(text) else " "
        if before in _BOUNDARY_CHARS and after in _BOUNDARY_CHARS:
            mentions.append((idx, idx + slen))
        start = idx + 1
    return mentions


def _expand_to_all_mentions(text: str, relex_entities: list[dict]) -> list[EntitySpan]:
    """Expand Relex entities to ALL surface mentions in the text.

    Relex detects entities at ONE position, but the syntactic relation may be
    expressed at a DIFFERENT occurrence. Without expanding to all mentions,
    FrameExtractor can never form the frame at the later occurrence.

    Overlapping spans from Relex are both included — FrameExtractor's slot
    resolution handles them.
    """
    seen_spans: set[tuple[int, int]] = set()
    all_spans: list[EntitySpan] = []

    for ent in relex_entities:
        surface = ent.get("text", "")
        if not surface.strip():
            continue
        for start, end in _find_all_mentions(text, surface):
            if (start, end) not in seen_spans:
                seen_spans.add((start, end))
                all_spans.append(EntitySpan(
                    surface=text[start:end],
                    start_char=start,
                    end_char=end,
                    entity_type="",
                ))

    # Always include the original Relex-detected spans
    for ent in relex_entities:
        s = int(ent.get("start", -1))
        e = int(ent.get("end", -1))
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
# Syntax evidence generation: FrameExtractor + SVO on ONE shared parse
# ---------------------------------------------------------------------------


def generate_syntax_records(
    text: str,
    relex_entities: list[dict],
    chunk_id: str,
    extractor: FrameExtractor,
    doc,
) -> tuple[list[dict], list[dict]]:
    """Run FrameExtractor + SVO on the caller-provided parse.

    ``doc`` is the ONE spaCy parse of ``text`` for this chunk (parse-once
    invariant). Returns (resolved_records, unmapped_records) where:
    - resolved_records: triples the resolver mapped to canonical predicates
    - unmapped_records: frames the resolver could not name (open-relation lane)
    """
    features = PRODUCTION_FEATURES

    # Expand Relex entities to ALL surface mentions so FrameExtractor can
    # form frames at any occurrence position.
    entity_spans = _expand_to_all_mentions(text, relex_entities)

    # Run FrameExtractor on the shared parse (no second nlp call).
    trace: list[dict] = []
    _disabled_groups: frozenset[str] = (
        frozenset() if features.verb_prep_frames
        else frozenset({"p2_verb_prep"})
    )
    try:
        triples = extractor.extract(
            text=text,
            entities=entity_spans,
            chunk_id=chunk_id,
            doc_id=chunk_id,
            doc=doc,
            trace=trace,
            disabled_feature_groups=_disabled_groups,
        )
    except Exception:
        triples = []

    resolved_records: list[dict] = []
    unmapped_records: list[dict] = []
    for t in triples:
        rec = {
            "chunk_id": chunk_id,
            "subject_start": t.subject_start,
            "subject_end": t.subject_end,
            "subject_text": t.subject_surface,
            "object_start": t.object_start,
            "object_end": t.object_end,
            "object_text": t.object_surface,
            "canonical_predicate": t.predicate,
            "surface_predicate": t.predicate_lemma,
            "pattern_id": t.dep_signature[:48] if t.dep_signature else "FRAME",
            "dependency_path": t.dep_signature,
            "lemma": t.predicate_lemma,
            "particle": "",
            "preposition": (
                t.dep_signature.split("prep:", 1)[1].split("-", 1)[0]
                if "prep:" in t.dep_signature else ""
            ),
            "confidence": t.confidence,
            "negated": t.polarity == "NEGATIVE",
            "polarity": t.polarity.lower(),
            "modality": t.modality.lower(),
            "attribution": t.assertion_mode,
            "temporal_cue": t.temporal_cue or "",
            "source": "frame",
            # Head-token offsets for genuine HEAD_TOKEN join (P2A)
            "subject_head_start": t.subject_head_start,
            "subject_head_end": t.subject_head_end,
            "object_head_start": t.object_head_start,
            "object_head_end": t.object_head_end,
            # Argument-role and direction provenance (open-relation routing)
            "subject_dependency_role": t.subject_dependency_role,
            "object_dependency_role": t.object_dependency_role,
            "voice": t.voice,
            "direction_source": t.direction_source,
            "direction_confidence": t.direction_confidence,
        }
        if t.predicate is not None:
            resolved_records.append(rec)
        else:
            # FrameExtractor emits unmapped frames (predicate=None,
            # mapping_status="UNMAPPED", graph_eligible=False). Route to the
            # open-relation lane.
            if features.open_relation_lane:
                rec["source"] = "frame_unmapped"
                rec["surface_predicate"] = t.predicate_lemma or t.predicate_surface
                rec["mapping_status"] = t.mapping_status
                rec["graph_eligible"] = t.graph_eligible
                unmapped_records.append(rec)

    # Run SVO on the SAME parse (parse-once invariant).
    try:
        svo_list = svo_candidates(doc)
    except Exception:
        svo_list = []

    span_index: list[tuple[int, int, str]] = [
        (es.start_char, es.end_char, es.surface) for es in entity_spans
    ]

    def _find_entity_for_token(tok) -> tuple[int, int, str] | None:
        subtree = list(tok.subtree)
        if subtree:
            tok_start = subtree[0].idx
            tok_end = subtree[-1].idx + len(subtree[-1].text)
        else:
            tok_start = tok.idx
            tok_end = tok.idx + len(tok.text)
        for s, e, surface in span_index:
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
        sig = "nsubjpass-VERB-dobj" if svo.passive else "nsubj-VERB-dobj"
        resolved = resolve_predicate(
            signature=sig, lemma=verb_lemma,
            subject_type="", object_type="",
        )
        if resolved is None:
            from services.extraction.dep_path_extractor import _load_synonyms
            syns = _load_synonyms()
            canonical = syns.get(verb_lemma)
            if canonical is None:
                continue
        else:
            canonical = resolved[0]

        resolved_records.append({
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
            "dependency_path": sig,
            "lemma": verb_lemma,
            "particle": "",
            "preposition": "",
            "confidence": 1.0,
            "negated": False,
            "polarity": "positive",
            "modality": "asserted",
            "attribution": "direct",
            "source": "svo",
        })

    # Credit/metadata fragment patterns (token-pattern lane)
    if features.credit_patterns:
        from services.extraction.credit_patterns import extract_credit_patterns
        entity_dicts = [
            {"start": es.start_char, "end": es.end_char,
             "text": es.surface, "type": es.entity_type}
            for es in entity_spans
        ]
        credit_records = extract_credit_patterns(text, entity_dicts, chunk_id)
        for rec in credit_records:
            # Credit patterns produce resolved predicates (created_by)
            resolved_records.append(rec)

    return resolved_records, unmapped_records


# ---------------------------------------------------------------------------
# Sentence-scope annotation
# ---------------------------------------------------------------------------

_SENTENCE_BOUNDARY_RE = re.compile(r'[.!?]+\s+')


def _sentence_boundaries(text: str) -> list[int]:
    """Return character offsets where sentences end (after punctuation + space)."""
    return [m.end() for m in _SENTENCE_BOUNDARY_RE.finditer(text)]


def _annotate_same_sentence(
    evidence: list[RelationEvidence],
    text: str,
    *,
    typed_links: bool = True,
) -> list[RelationEvidence]:
    """Set same_sentence=False for pairs whose spans cross a sentence boundary.

    A pair is cross-sentence when at least one sentence boundary falls
    strictly between the earlier span's end and the later span's start.

    Also computes sentence_distance (number of boundaries in the gap) and
    has_cross_sentence_link (entity repetition signal).

    typed_links: when False, uses the safe pre-feature policy —
    cross-sentence pairs get has_cross_sentence_link=False and
    cross_sentence_link_type="none" so no typed link may authorize
    additional handling.
    """
    boundaries = _sentence_boundaries(text)
    if not boundaries:
        return evidence  # single-sentence text — everything is same-sentence

    # Pre-compute entity repetition for cross-sentence link detection.
    entity_surfaces: set[str] = set()
    for ev in evidence:
        entity_surfaces.add(ev.subject_text)
        entity_surfaces.add(ev.object_text)
    repeated_surfaces: set[str] = {
        s for s in entity_surfaces
        if s and text.count(s) >= 2
    }

    result: list[RelationEvidence] = []
    for ev in evidence:
        # Determine the gap between the two spans
        if ev.subject_start <= ev.object_start:
            gap_start, gap_end = ev.subject_end, ev.object_start
        else:
            gap_start, gap_end = ev.object_end, ev.subject_start

        # Count sentence boundaries in the gap = sentence distance
        distance = sum(1 for b in boundaries if gap_start < b < gap_end)
        if distance > 0 and ev.same_sentence:
            if typed_links:
                has_link = (
                    ev.subject_text in repeated_surfaces
                    or ev.object_text in repeated_surfaces
                )
                link_type = (
                    "exact_entity_repeat" if has_link else "none"
                )
            else:
                has_link = False
                link_type = "none"
            result.append(replace(
                ev,
                same_sentence=False,
                sentence_distance=distance,
                has_cross_sentence_link=has_link,
                cross_sentence_link_type=link_type,
            ))
        else:
            result.append(ev)
    return result


# ---------------------------------------------------------------------------
# Build union evidence set
# ---------------------------------------------------------------------------


def build_union_evidence(
    chunk_id: str,
    prediction_row: dict,
    resolved_syntax: list[dict],
    unmapped_syntax: list[dict],
    text: str = "",
) -> list[RelationEvidence]:
    """Build the union of Relex-scored pairs and FrameExtractor triples.

    1. Build Relex RelationEvidence from the sidecar prediction row
    2. Join all syntax records (resolved + unmapped) to Relex evidence
    3. Find syntax records with no matching Relex pair → syntax-only evidence
    4. Annotate same_sentence for pair-scope gating

    Production lane: endpoint types come exclusively from the prediction
    row. There is NO oracle/gold type injection path here.
    """
    features = PRODUCTION_FEATURES
    all_syntax = resolved_syntax + unmapped_syntax

    # 1. Build Relex evidence
    relex_evidence = build_relation_evidence(chunk_id, prediction_row)

    # 2. Join syntax to Relex evidence
    joined = join_syntax_evidence(
        relex_evidence, all_syntax,
        surface_join=True,
        enable_head_token=features.head_token_join,
    )

    # 3. Find unmatched syntax records → syntax-only evidence
    # Build set of canonical entity pairs covered by Relex
    relex_entity_pairs: set[tuple[str, str]] = set()
    for ev in relex_evidence:
        subj_norm = normalized_mention_cached(ev.subject_text)
        obj_norm = normalized_mention_cached(ev.object_text)
        if subj_norm and obj_norm:
            relex_entity_pairs.add((subj_norm, obj_norm))

    # Find syntax records whose entity pair is NOT in Relex
    syntax_only_evidence: list[RelationEvidence] = []
    seen_spans: set[tuple[int, int, int, int]] = set()
    for rec in all_syntax:
        s_start = int(rec.get("subject_start", -1))
        s_end = int(rec.get("subject_end", -1))
        o_start = int(rec.get("object_start", -1))
        o_end = int(rec.get("object_end", -1))
        span_key = (s_start, s_end, o_start, o_end)
        if span_key in seen_spans:
            continue

        subj_text = rec.get("subject_text", "")
        obj_text = rec.get("object_text", "")
        subj_norm = normalized_mention_cached(subj_text)
        obj_norm = normalized_mention_cached(obj_text)
        if subj_norm and obj_norm and (subj_norm, obj_norm) in relex_entity_pairs:
            continue  # this pair has a Relex counterpart

        seen_spans.add(span_key)
        syntax_only_evidence.append(
            RelationEvidence(
                chunk_id=chunk_id,
                subject_id=f"span:{s_start}:{s_end}",
                subject_text=subj_text,
                subject_type="unknown",
                subject_start=s_start,
                subject_end=s_end,
                object_id=f"span:{o_start}:{o_end}",
                object_text=obj_text,
                object_type="unknown",
                object_start=o_start,
                object_end=o_end,
                predicate_scores=(),
                reverse_scores={},
                syntax_evidence=(
                    SyntaxEvidence(
                        canonical_predicate=str(rec.get("canonical_predicate") or ""),
                        surface_predicate=str(rec.get("surface_predicate") or ""),
                        pattern_id=str(rec.get("pattern_id", "UNKNOWN")),
                        confidence=float(rec.get("confidence", 1.0)),
                        negated=bool(rec.get("negated", False)),
                        source_family="spacy_dependency_parse",
                        join_mode="exact_span",
                        subject_dependency_role=str(rec.get("subject_dependency_role", "")),
                        object_dependency_role=str(rec.get("object_dependency_role", "")),
                        voice=str(rec.get("voice", "")),
                        direction_source=str(rec.get("direction_source", "")),
                        direction_confidence=str(rec.get("direction_confidence", "")),
                    ),
                ),
                same_sentence=True,  # syntax evidence implies same-sentence link
            )
        )

    union = joined + syntax_only_evidence

    # 4. Annotate same_sentence for Relex-anchored pairs
    if text:
        union = _annotate_same_sentence(
            union, text,
            typed_links=features.typed_cross_sentence_links,
        )

    return union

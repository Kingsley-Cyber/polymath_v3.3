#!/usr/bin/env python3
"""Unified shadow pipeline: Relex Large + FrameExtractor union evaluation.

Architecture per sample:
  1. Load Relex predictions: entities (spans, no types) + all ordered pairs × 28 labels
  2. spaCy parse once
  3. Run FrameExtractor with trace on Relex entity spans
  4. Run native SVO on the same parse
  5. Build syntax records (resolved triples + unmapped frames + SVO)
  6. Build Relex RelationEvidence from predictions
  7. Join syntax to Relex evidence (4-level join)
  8. Find syntax records with no matching Relex pair → syntax-only evidence
  9. Evaluate the UNION through the corroboration gate
 10. Classify each pair by evidence source and match against gold

The UNION is the key difference from the old shadow script: every FrameExtractor
triple gets a gate decision even when Relex has no matching pair. The gate's
syntax-only entry path (ACCEPT_CORROBORATED / STORE_UNMAPPED_SURFACE_RELATION)
handles these.

Output: evidence source table, core metrics, per-status breakdown, determinism
hash, unmapped surface relation collection.

Run: cd backend && python scripts/unified_shadow_pipeline.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import replace
from pathlib import Path

# Ensure backend/ is on the path
REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from services.extraction.canonical import (
    canonicalize_predicate_label,
    entity_id_from_name,
)
from services.extraction.corroboration_gate import evaluate_relation, load_policy
from services.extraction.dep_path_extractor import EntitySpan, resolve_predicate
from services.extraction.frame_extractor import FrameExtractor
from services.extraction.mention_normalizer import (
    is_morphological_variant,
    normalized_mention_cached,
)
from services.extraction.relex_adapter import (
    build_relation_evidence,
    join_syntax_evidence,
)
from services.extraction.relation_evidence import (
    GateDecision,
    GateStatus,
    PredicateScore,
    RelationEvidence,
    SyntaxEvidence,
)
from services.extraction.svo_candidates import svo_candidates

GOLD_PATH = REPO_ROOT / "data/deterministic_gold_score/data/relex_gold_v1.jsonl"
PRED_PATH_V1 = REPO_ROOT / "data/deterministic_gold_score/data/relex_large_v1.predictions.jsonl"
PRED_PATH_V2 = REPO_ROOT / "data/deterministic_gold_score/data/relex_large_v2.predictions.jsonl"
PRED_PATH_V3_MPS = REPO_ROOT / "data/deterministic_gold_score/data/relex_large_v3_mps_fp32.predictions.jsonl"
PRED_PATH = PRED_PATH_V3_MPS  # default to v3 MPS FP32; use --pred-v1 for old null artifact
UNMAPPED_OUTPUT = REPO_ROOT / "data/unmapped_surface_relations.jsonl"
RESULTS_OUTPUT = REPO_ROOT / "data/shadow_pipeline_results.jsonl"

# Ablation suppression diagnostics: records candidates that were NOT emitted
# because a feature was disabled. Explains what disappeared between profiles.
# Run-local: instantiated per pipeline run, not shared across runs.
class SuppressionRecorder:
    """Run-local recorder for ablation-suppressed candidates."""

    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: list[dict] = []

    def record(self, component: str, **fields) -> None:
        self._records.append({"component": component, **fields})

    @property
    def records(self) -> list[dict]:
        return self._records

    def __len__(self) -> int:
        return len(self._records)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            obj = json.loads(line)
            # Skip metadata header lines (v2 schema)
            if "__metadata__" in obj:
                continue
            rows.append(obj)
    return rows


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
    expressed at a DIFFERENT occurrence. Example: gold CAPTCHA is at position 62
    (sentence 1), but the "Companies use CAPTCHA" relation is in sentence 2 at a
    different CAPTCHA position. Without expanding to all mentions, FrameExtractor
    can never form the frame for sentence 2.

    Overlapping spans from Relex (e.g. "CAPTCHA" [62,69] and "CAPTCHA fields"
    [62,76]) are both included — FrameExtractor's slot resolution handles them.
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
# Syntax evidence generation: FrameExtractor + SVO
# ---------------------------------------------------------------------------


def generate_unified_syntax(
    text: str,
    relex_entities: list[dict],
    chunk_id: str,
    extractor: FrameExtractor,
    *,
    features=None,
    suppressions=None,
) -> tuple[list[dict], list[dict]]:
    """Run FrameExtractor + SVO and produce syntax records.

    Returns (resolved_records, unmapped_records) where:
    - resolved_records: triples the resolver mapped to canonical predicates
    - unmapped_records: frames the resolver could not name (T4 DROP)

    features: ExtractionFeatures controlling ablation switches.
    suppressions: SuppressionRecorder for ablation diagnostics.
    """
    from services.extraction.ablation import PRODUCTION_FEATURES
    if features is None:
        features = PRODUCTION_FEATURES
    if suppressions is None:
        suppressions = SuppressionRecorder()
    # Expand Relex entities to ALL surface mentions so FrameExtractor can
    # form frames at any occurrence position.
    entity_spans = _expand_to_all_mentions(text, relex_entities)

    # Run FrameExtractor with trace to capture unmapped frames
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
            trace=trace,
            disabled_feature_groups=_disabled_groups,
        )
    except Exception as exc:
        print(f"  [warn] FrameExtractor failed for {chunk_id}: {exc}")
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
            "confidence": t.confidence,
            "negated": t.polarity == "NEGATIVE",
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
            # P2A: FrameExtractor emits unmapped frames (predicate=None,
            # mapping_status="UNMAPPED", graph_eligible=False) instead of
            # dropping them. Route to the open-relation lane.
            if features.open_relation_lane:
                rec["source"] = "frame_unmapped"
                rec["surface_predicate"] = t.predicate_lemma or t.predicate_surface
                rec["mapping_status"] = t.mapping_status
                rec["graph_eligible"] = t.graph_eligible
                unmapped_records.append(rec)
            else:
                # Ablation suppression diagnostic: record what disappeared
                suppressions.record(
                    "open_relation_lane",
                    chunk_id=chunk_id,
                    subject=t.subject_surface,
                    object=t.object_surface,
                    surface_predicate=t.predicate_lemma or t.predicate_surface,
                )

    # Run SVO on the same parse
    try:
        doc = extractor._nlp(text)
        svo_list = svo_candidates(doc)
    except Exception as exc:
        print(f"  [warn] SVO failed for {chunk_id}: {exc}")
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
            "confidence": 1.0,
            "negated": False,
            "source": "svo",
        })

    # --- P2B: Credit/metadata fragment patterns (token-pattern lane) ---
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
    # else: credit patterns disabled — component not invoked, no latency cost

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

    P4-lite: also computes sentence_distance (number of boundaries in the gap)
    and has_cross_sentence_link (entity repetition signal).

    typed_links: when False, uses the safe pre-feature policy —
    cross-sentence pairs get has_cross_sentence_link=False and
    cross_sentence_link_type="none" so no typed link may authorize
    additional handling. The gate still sees REVIEW_CROSS_SENTENCE
    or REJECT_SCOPE via sentence_distance alone.
    """
    boundaries = _sentence_boundaries(text)
    if not boundaries:
        return evidence  # single-sentence text — everything is same-sentence

    # Pre-compute entity repetition for cross-sentence link detection.
    # An entity surface that appears 2+ times in the text is a repeated-entity
    # link signal (the simplest explicit discourse link).
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
            # Cross-sentence link: either endpoint surface is repeated
            # elsewhere in the text (exact repeated entity signal).
            if typed_links:
                has_link = (
                    ev.subject_text in repeated_surfaces
                    or ev.object_text in repeated_surfaces
                )
                # P4-lite hardening: typed link classification.
                # Currently only EXACT_ENTITY_REPEAT is detected. Future:
                # RESOLVED_ALIAS, VALIDATED_COREFERENCE, HEADING_CONTINUATION,
                # LIST_CONTINUATION, DOCUMENT_STRUCTURE_LINK.
                link_type = (
                    "exact_entity_repeat" if has_link else "none"
                )
            else:
                # Safe pre-feature policy: no typed link may authorize
                # additional handling. Cross-sentence pairs still get
                # REVIEW_CROSS_SENTENCE or REJECT_SCOPE via distance alone.
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
    sid: str,
    pred: dict,
    resolved_syntax: list[dict],
    unmapped_syntax: list[dict],
    text: str = "",
    oracle_types: dict[tuple[int, int], str] | None = None,
    *,
    features=None,
) -> list[RelationEvidence]:
    """Build the union of Relex-scored pairs and FrameExtractor triples.

    1. Build Relex RelationEvidence from predictions
    2. Join all syntax records (resolved + unmapped) to Relex evidence
    3. Find syntax records with no matching Relex pair → syntax-only evidence
    4. Annotate same_sentence for pair-scope gating

    oracle_types: optional (start, end) → type mapping injected from gold
    entity annotations for diagnostic replay. NEVER used in production.
    features: ExtractionFeatures controlling ablation switches.
    """
    from services.extraction.ablation import PRODUCTION_FEATURES
    if features is None:
        features = PRODUCTION_FEATURES
    all_syntax = resolved_syntax + unmapped_syntax

    # 1. Build Relex evidence (with oracle types if provided)
    relex_evidence = build_relation_evidence(
        sid, pred, oracle_types=oracle_types,
    )

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
                chunk_id=sid,
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


# ---------------------------------------------------------------------------
# Gold matching
# ---------------------------------------------------------------------------


def _gold_relations_for_sample(sample: dict) -> list[dict]:
    """Extract gold relations with entity text/spans."""
    entity_map = {e["entity_id"]: e for e in sample.get("entities", [])}
    rels = []
    for rel in sample.get("relations", []):
        head_ent = entity_map.get(rel["head_id"], {})
        tail_ent = entity_map.get(rel["tail_id"], {})
        rels.append({
            **rel,
            "head_start": head_ent.get("start", -1),
            "head_end": head_ent.get("end", -1),
            "head_text": head_ent.get("text", ""),
            "tail_start": tail_ent.get("start", -1),
            "tail_end": tail_ent.get("end", -1),
            "tail_text": tail_ent.get("text", ""),
        })
    return rels


def _surface_match(ev_text: str, gold_text: str) -> bool:
    """Check if an entity surface matches a gold entity surface.

    Only exact (case-insensitive) and morphological-variant matches pass.
    Substring matching (contains / startswith) is NOT allowed — it causes
    false alignments like "story" matching "A CHRISTMAS STORY".
    """
    a = ev_text.lower().strip()
    b = gold_text.lower().strip()
    if a == b:
        return True
    if is_morphological_variant(a, b):
        return True
    return False


def _offset_match(
    ev_start: int, ev_end: int,
    gold_offsets: list[tuple[int, int]],
) -> bool:
    """Check if an entity span matches any gold offset exactly."""
    return any(ev_start == gs and ev_end == ge for gs, ge in gold_offsets)


def _compute_gold_offsets(
    text: str, gold_rels: list[dict],
) -> list[dict]:
    """Pre-compute character offsets for each gold relation's head/tail.

    Returns a list parallel to gold_rels, each entry containing
    {"head_offsets": [(start, end), ...], "tail_offsets": [(start, end), ...]}.
    """
    result = []
    for gr in gold_rels:
        result.append({
            "head_offsets": _find_all_mentions(text, gr.get("head_text", "")),
            "tail_offsets": _find_all_mentions(text, gr.get("tail_text", "")),
        })
    return result


def _matches_gold(
    ev: RelationEvidence,
    gold_rel: dict,
    gold_offsets: dict | None = None,
) -> bool:
    """Check if an evaluated pair matches a gold relation.

    Alignment priority (per P0 evaluator integrity):
      1. Exact character offsets (when gold_offsets provided)
      2. Exact surface match (case-insensitive, no substring)
    """
    if gold_offsets is not None:
        head_ok = _offset_match(
            ev.subject_start, ev.subject_end,
            gold_offsets["head_offsets"],
        )
        tail_ok = _offset_match(
            ev.object_start, ev.object_end,
            gold_offsets["tail_offsets"],
        )
        if head_ok and tail_ok:
            return True
        # If offsets were found for both but don't match, this is NOT
        # a match — do not fall through to surface matching.
        if gold_offsets["head_offsets"] and gold_offsets["tail_offsets"]:
            return False
    # Fallback: exact surface match (no substring)
    g_head = gold_rel.get("head_text", "")
    g_tail = gold_rel.get("tail_text", "")
    return (
        _surface_match(ev.subject_text, g_head)
        and _surface_match(ev.object_text, g_tail)
    )


# ---------------------------------------------------------------------------
# Determinism hash
# ---------------------------------------------------------------------------


def _decision_hash(decision: GateDecision) -> str:
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
# Oracle type map (evaluation only — NEVER production)
# ---------------------------------------------------------------------------


def build_oracle_type_map(
    gold_sample: dict,
    text: str,
) -> dict[tuple[int, int], str]:
    """Build (start, end) → type mapping from gold entity annotations.

    Gold entities carry authoritative type labels. This map is injected into
    the adapter for diagnostic Replay B (oracle types) to measure the upper
    bound of type repair. The oracle ONLY applies to spans that exist in the
    gold annotation — prediction entities at non-gold offsets remain unknown.

    Additionally, for prediction entities whose text matches a gold entity
    text but whose offsets differ (model detected the entity at a different
    position), we also include the prediction entity's offsets mapped to
    the gold type. This handles the common case where the model detects the
    same entity at a slightly different span.
    """
    oracle: dict[tuple[int, int], str] = {}

    # Direct gold offsets
    gold_type_by_text: dict[str, str] = {}
    for ent in gold_sample.get("entities", []):
        start = int(ent.get("start", -1))
        end = int(ent.get("end", -1))
        etype = ent.get("type", "")
        if start >= 0 and end > start and etype:
            oracle[(start, end)] = etype
            gold_type_by_text[ent.get("text", "").lower()] = etype

    # Extend to prediction entity offsets that match gold entity text
    # (handles model detecting the same entity at a different position)
    if gold_type_by_text:
        for ent in gold_sample.get("_pred_entities", []):
            ent_text = ent.get("text", "").lower()
            if ent_text in gold_type_by_text:
                start = int(ent.get("start", -1))
                end = int(ent.get("end", -1))
                if start >= 0 and end > start:
                    oracle[(start, end)] = gold_type_by_text[ent_text]

    return oracle


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    oracle_mode = "--oracle" in sys.argv
    bypass_types = "--bypass-types" in sys.argv
    compare_mode = "--compare" in sys.argv or oracle_mode or bypass_types
    use_v1 = "--pred-v1" in sys.argv

    # --- Ablation profile (centralized, immutable) ---
    from services.extraction.ablation import resolve_profile, ExtractionFeatures
    profile_name: str | None = None
    output_dir: Path | None = None
    for i, arg in enumerate(sys.argv):
        if arg == "--ablation-profile" and i + 1 < len(sys.argv):
            profile_name = sys.argv[i + 1]
        elif arg == "--output-dir" and i + 1 < len(sys.argv):
            output_dir = Path(sys.argv[i + 1])
    features = resolve_profile(profile_name)

    pred_path = PRED_PATH_V1 if use_v1 else PRED_PATH
    gold_samples = {s["sample_id"]: s for s in load_jsonl(GOLD_PATH)}
    predictions = {p["sample_id"]: p for p in load_jsonl(pred_path)}

    print("Loading policy and FrameExtractor...")
    policy = load_policy()
    extractor = FrameExtractor()

    # Collect gold relations
    all_gold: list[dict] = []
    for sid, sample in gold_samples.items():
        for gr in _gold_relations_for_sample(sample):
            gr["sample_id"] = sid
            all_gold.append(gr)

    print(f"\nGold relations: {len(all_gold)}")
    print(f"Oracle mode: {oracle_mode}  Bypass types: {bypass_types}")
    print(f"Ablation profile: {profile_name or 'FULL (all features)'}")
    print(f"  Features: {features}")
    print(f"{'=' * 120}")

    # Run pipeline in both modes for comparison
    normal_results, normal_suppressions = _run_pipeline(
        gold_samples, predictions, all_gold, policy, extractor,
        oracle_mode=False,
        features=features,
    )
    if compare_mode:
        oracle_results, _ = _run_pipeline(
            gold_samples, predictions, all_gold, policy, extractor,
            oracle_mode=True,
            bypass_types=bypass_types,
        )
        mode_label = "BYPASS_TYPES" if bypass_types else "ORACLE"
        _print_comparison(normal_results, oracle_results, all_gold,
                          oracle_label=mode_label)
    else:
        _print_summary(normal_results, all_gold)

    # Write unmapped surface relations for ontology-extension analysis (P2A)
    # Includes both STORE (trusted direction) and REVIEW (ambiguous direction).
    unmapped_decisions = [
        r for r in normal_results
        if r["status"] in ("store_unmapped_surface_relation", "review_unmapped_relation")
    ]
    if unmapped_decisions:
        UNMAPPED_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        with UNMAPPED_OUTPUT.open("w", encoding="utf-8") as fh:
            for r in unmapped_decisions:
                # Reflect actual routing decision in the output
                decision_label = (
                    "STORE_UNMAPPED_SURFACE_RELATION"
                    if r["status"] == "store_unmapped_surface_relation"
                    else "REVIEW_UNMAPPED_RELATION"
                )
                fh.write(json.dumps({
                    "sample_id": r["sid"],
                    "subject": r["subject"],
                    "object": r["object"],
                    "surface_predicate": r.get("surface_predicate", ""),
                    "predicate_lemma": r.get("predicate_lemma", ""),
                    "canonical_predicate": None,
                    "decision_candidate": decision_label,
                    "pattern_id": r.get("pattern_id", ""),
                    "score": r.get("score", 0.0),
                    # Argument-role and direction provenance
                    "subject_dependency_role": r.get("subject_dependency_role", ""),
                    "object_dependency_role": r.get("object_dependency_role", ""),
                    "voice": r.get("voice", ""),
                    "direction_source": r.get("direction_source", ""),
                    "direction_confidence": r.get("direction_confidence", ""),
                }, ensure_ascii=False) + "\n")
        n_store = sum(1 for r in unmapped_decisions if r["status"] == "store_unmapped_surface_relation")
        n_review = sum(1 for r in unmapped_decisions if r["status"] == "review_unmapped_relation")
        print(f"\n  Unmapped surface relations: {len(unmapped_decisions)} "
              f"(STORE={n_store}, REVIEW={n_review}) → {UNMAPPED_OUTPUT}")

    # Write full results JSONL for P3 confusion matrix and downstream tooling
    RESULTS_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS_OUTPUT.open("w", encoding="utf-8") as fh:
        for r in normal_results:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    print(f"  Full results: {len(normal_results)} → {RESULTS_OUTPUT}")

    # --- Ablation output directory (manifest + artifacts) ---
    if output_dir is not None:
        _write_ablation_outputs(
            output_dir=output_dir,
            profile_name=profile_name,
            features=features,
            pred_path=pred_path,
            results=normal_results,
            all_gold=all_gold,
            suppressions=normal_suppressions,
        )


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_commit() -> str:
    """Return the current git commit hash, or 'unknown'."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:  # noqa: BLE001
        return "unknown"


def _write_ablation_outputs(
    *,
    output_dir: Path,
    profile_name: str | None,
    features,
    pred_path: Path,
    results: list[dict],
    all_gold: list[dict],
    suppressions=None,
) -> None:
    """Write manifest.json, results.jsonl, summary.json, and diagnostic artifacts.

    Each ablation run produces a self-contained output directory that can be
    compared across profiles without re-running the pipeline.
    """
    from services.extraction.ablation import ABLATION_PROFILES
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- manifest.json ---
    ontology_path = REPO_ROOT / "config" / "ontology.yaml"
    policy_path = BACKEND / "registries" / "acceptance_policy.yaml"
    manifest = {
        "ablation_profile": profile_name or "FULL",
        "features": {
            "head_token_join": features.head_token_join,
            "open_relation_lane": features.open_relation_lane,
            "credit_patterns": features.credit_patterns,
            "verb_prep_frames": features.verb_prep_frames,
            "typed_cross_sentence_links": features.typed_cross_sentence_links,
        },
        "predictions_artifact": str(pred_path),
        "predictions_sha256": _sha256_file(pred_path) if pred_path.exists() else "missing",
        "gold_artifact_sha256": _sha256_file(GOLD_PATH) if GOLD_PATH.exists() else "missing",
        "ontology_sha256": _sha256_file(ontology_path) if ontology_path.exists() else "missing",
        "acceptance_policy_sha256": _sha256_file(policy_path) if policy_path.exists() else "missing",
        "code_commit": _git_commit(),
        "closed_world": False,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # --- results.jsonl ---
    with (output_dir / "results.jsonl").open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # --- summary.json ---
    accepted = [r for r in results if r.get("is_accepted")]
    shadow = [r for r in results if r.get("is_shadow")]
    review = [r for r in results if r.get("is_review_cross") or r.get("is_review_unknown")]
    scope_rej = [r for r in results if r.get("status") == "reject_scope"]
    correct = [r for r in results if r.get("is_correct")]
    open_rels = [r for r in results if r.get("status") in ("store_unmapped_surface_relation", "review_unmapped_relation")]

    # Gold recall: unique gold relations matched by ANY output record
    # (alignment by exact offsets, not canonical entity IDs or surface strings)
    gold_pair_keys = set()       # any status — pair identity (offsets)
    gold_triple_keys = set()     # pair + correct predicate
    gold_accepted_keys = set()   # accepted + correct predicate
    gold_shadow_keys = set()     # shadow + correct predicate
    syntax_eligible_gold = 0
    syntax_covered_gold = 0
    for r in results:
        pk = r.get("gold_pair_key")
        if not pk:
            continue
        gold_pair_keys.add(pk)
        if r.get("pred_match"):
            tk = r.get("gold_triple_key", pk)
            gold_triple_keys.add(tk)
            if r.get("is_accepted"):
                gold_accepted_keys.add(tk)
            if r.get("is_shadow"):
                gold_shadow_keys.add(tk)
        # Syntax coverage on gold-matched pairs
        if r.get("has_syntax"):
            syntax_eligible_gold += 1
            syntax_covered_gold += 1
        elif r.get("matched_gold"):
            syntax_eligible_gold += 1

    total_gold = max(len(all_gold), 1)
    summary = {
        "profile": profile_name or "FULL",
        "total_pairs": len(results),
        "accepted_count": len(accepted),
        "shadow_count": len(shadow),
        "review_count": len(review),
        "scope_rejection_count": len(scope_rej),
        "open_relation_count": len(open_rels),
        "correct_accepted": len(correct),
        "gold_total": len(all_gold),
        # --- Gold-aligned recall metrics ---
        "gold_pair_recall": len(gold_pair_keys) / total_gold,
        "gold_triple_recall": len(gold_triple_keys) / total_gold,
        "gold_predicate_accuracy": (
            len(gold_triple_keys) / max(len(gold_pair_keys), 1)
        ),
        "gold_accepted_recall": len(gold_accepted_keys) / total_gold,
        "gold_shadow_recall": len(gold_shadow_keys) / total_gold,
        "accepted_labeled_hit_rate": (
            len(correct) / max(len(accepted), 1)
        ),
        "shadow_labeled_hit_rate": (
            len([r for r in shadow if r.get("is_shadow_correct")])
            / max(len(shadow), 1)
        ),
        "syntax_coverage_on_gold": (
            syntax_covered_gold / max(syntax_eligible_gold, 1)
        ),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # --- gold_traces.jsonl (all candidates matched to gold, any status) ---
    with (output_dir / "gold_traces.jsonl").open("w", encoding="utf-8") as fh:
        for r in results:
            if r.get("matched_gold"):
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # --- accepted_candidates.jsonl ---
    with (output_dir / "accepted_candidates.jsonl").open("w", encoding="utf-8") as fh:
        for r in accepted:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # --- open_relations.jsonl ---
    with (output_dir / "open_relations.jsonl").open("w", encoding="utf-8") as fh:
        for r in open_rels:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")

    # --- scope_diagnostics.json ---
    scope_diag = {
        "cross_sentence_pairs": len([
            r for r in results if not r.get("same_sentence", True)
        ]),
        "scope_rejections": len(scope_rej),
        "review_cross_sentence": len([
            r for r in results if r.get("is_review_cross")
        ]),
    }
    (output_dir / "scope_diagnostics.json").write_text(
        json.dumps(scope_diag, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # --- suppressed_candidates.jsonl (ablation diagnostics) ---
    if suppressions:
        with (output_dir / "suppressed_candidates.jsonl").open("w", encoding="utf-8") as fh:
            for s in suppressions.records:
                fh.write(json.dumps(s, ensure_ascii=False, default=str) + "\n")

    print(f"\n  Ablation outputs → {output_dir}/")
    print(f"    manifest.json, results.jsonl, summary.json,")
    print(f"    gold_traces.jsonl, accepted_candidates.jsonl,")
    print(f"    open_relations.jsonl, scope_diagnostics.json")
    if suppressions:
        print(f"    suppressed_candidates.jsonl ({len(suppressions)} records)")

def _run_pipeline(
    gold_samples: dict[str, dict],
    predictions: dict[str, dict],
    all_gold: list[dict],
    policy,
    extractor: FrameExtractor,
    *,
    oracle_mode: bool = False,
    bypass_types: bool = False,
    features=None,
) -> list[dict]:
    """Run the full pipeline and return per-pair results.

    oracle_mode: inject gold entity types into the adapter (Replay B).
    bypass_types: assume all endpoint types are valid — removes
        allowed_types constraints AND REVIEW_UNKNOWN_TYPE downgrade.
        Shows the theoretical maximum recall if types were perfect.
    features: ExtractionFeatures controlling ablation switches.
        None → all features enabled (production).
    """
    from services.extraction.ablation import PRODUCTION_FEATURES
    if features is None:
        features = PRODUCTION_FEATURES
    mode_label = "BYPASS_TYPES" if bypass_types else ("ORACLE" if oracle_mode else "NORMAL")
    print(f"\n[{mode_label}] Running pipeline...")

    # Reset ablation suppression diagnostics for this run
    suppressions = SuppressionRecorder()

    # In bypass mode, strip allowed_types from all predicate policies so
    # the type constraint gate never fires. This simulates a world where
    # all endpoint types are valid and specific.
    if bypass_types:
        from services.extraction.corroboration_gate import RelationPolicy, PredicatePolicy
        bypass_policy = _make_bypass_policy(policy)
    else:
        bypass_policy = policy

    results: list[dict] = []
    run_hashes: list[str] = []
    total_eval_ms: list[float] = []

    for sid, sample in gold_samples.items():
        pred = predictions.get(sid)
        if pred is None:
            continue

        text = sample["text"]
        gold_rels = _gold_relations_for_sample(sample)
        gold_offsets_list = _compute_gold_offsets(text, gold_rels)

        # Expand Relex entities to all mentions for FrameExtractor
        raw_entities = pred.get("entities", [])

        # Build oracle type map if in oracle mode
        oracle_types = None
        if oracle_mode:
            # Attach prediction entities to gold sample for text-based matching
            sample_with_pred = {**sample, "_pred_entities": raw_entities}
            oracle_types = build_oracle_type_map(sample_with_pred, text)

        # Generate syntax evidence
        resolved_syntax, unmapped_syntax = generate_unified_syntax(
            text, raw_entities, sid, extractor,
            features=features, suppressions=suppressions,
        )

        # Build union evidence
        union_evidence = build_union_evidence(
            sid, pred, resolved_syntax, unmapped_syntax, text=text,
            oracle_types=oracle_types, features=features,
        )

        # Evaluate each pair in the union
        for ev in union_evidence:
            _t0 = time.perf_counter()
            decision = evaluate_relation(ev, bypass_policy)
            _eval_ms = (time.perf_counter() - _t0) * 1000
            total_eval_ms.append(_eval_ms)
            run_hashes.append(_decision_hash(decision))

            # Classify evidence source
            has_relex = len(ev.predicate_scores) > 0
            has_syntax = len(ev.syntax_evidence) > 0
            syntax_preds = [
                s.canonical_predicate for s in ev.syntax_evidence
                if s.canonical_predicate and s.canonical_predicate.strip()
            ]

            if has_relex and has_syntax:
                top_pred = max(ev.predicate_scores, key=lambda p: p.score).predicate
                if syntax_preds and top_pred in syntax_preds:
                    source = "both_agree"
                elif syntax_preds:
                    source = "both_disagree"
                else:
                    source = "relex_only"
            elif has_relex:
                source = "relex_only"
            elif has_syntax:
                source = "frame_only"
            else:
                source = "neither"

            # Check gold match (offset-based alignment)
            matched_gold = None
            matched_gold_idx = -1
            for gi, gr in enumerate(gold_rels):
                if _matches_gold(ev, gr, gold_offsets_list[gi]):
                    matched_gold = gr
                    matched_gold_idx = gi
                    break

            # Unique keys for this gold relation (exact-offset identity)
            # Pair key: sample + subject offsets + object offsets (direction preserved)
            # Triple key: pair key + canonical predicate
            if matched_gold:
                gold_pair_key = (
                    f"{sid}|{matched_gold['head_start']}|{matched_gold['head_end']}"
                    f"|{matched_gold['tail_start']}|{matched_gold['tail_end']}"
                )
                gold_triple_key = f"{gold_pair_key}|{matched_gold['predicate']}"
            else:
                gold_pair_key = None
                gold_triple_key = None

            pred_match = (
                decision.predicate == matched_gold["predicate"]
                if matched_gold and decision.predicate
                else False
            )

            is_accepted = decision.status in (
                GateStatus.ACCEPT_HIGH, GateStatus.ACCEPT_CORROBORATED,
                GateStatus.ACCEPT_SYNTAX_HIGH,
            )
            is_shadow = decision.status == GateStatus.SHADOW_RELEX_HIGH
            is_review_unknown = decision.status == GateStatus.REVIEW_UNKNOWN_TYPE
            is_review_cross = decision.status == GateStatus.REVIEW_CROSS_SENTENCE
            is_correct = is_accepted and pred_match and matched_gold is not None
            is_shadow_correct = is_shadow and pred_match and matched_gold is not None
            is_review_correct = is_review_unknown and pred_match and matched_gold is not None
            is_cross_correct = is_review_cross and pred_match and matched_gold is not None

            results.append({
                "sid": sid,
                "subject": ev.subject_text,
                "object": ev.object_text,
                "canonical_subject_id": ev.canonical_subject_id,
                "canonical_object_id": ev.canonical_object_id,
                "gold_pred": matched_gold["predicate"] if matched_gold else None,
                "gate_pred": decision.predicate,
                "status": decision.status.value,
                "score": max((p.score for p in ev.predicate_scores), default=0.0),
                "source": source,
                "has_relex": has_relex,
                "has_syntax": has_syntax,
                "matched_gold": matched_gold is not None,
                "gold_pair_key": gold_pair_key,
                "gold_triple_key": gold_triple_key,
                "pred_match": pred_match,
                "is_accepted": is_accepted,
                "is_shadow": is_shadow,
                "is_review_unknown": is_review_unknown,
                "is_review_cross": is_review_cross,
                "is_correct": is_correct,
                "is_shadow_correct": is_shadow_correct,
                "is_review_correct": is_review_correct,
                "is_cross_correct": is_cross_correct,
                "triple": f"{ev.subject_text} → {decision.predicate or '?'} → {ev.object_text}",
                # Type provenance diagnostics
                "subject_type": ev.subject_type,
                "object_type": ev.object_type,
                "type_status": ev.effective_type_status,
                "type_failure_stage": ev.type_failure_stage,
                # P4-lite scope diagnostics
                "sentence_distance": ev.sentence_distance,
                "has_cross_sentence_link": ev.has_cross_sentence_link,
                "cross_sentence_link_type": ev.cross_sentence_link_type,
                "blocking_reasons": list(decision.blocking_reasons),
                # P2A open-relation diagnostics
                "surface_predicate": (
                    ev.syntax_evidence[0].surface_predicate
                    if ev.syntax_evidence else ""
                ),
                "pattern_id": (
                    ev.syntax_evidence[0].pattern_id
                    if ev.syntax_evidence else ""
                ),
                # Argument-role and direction provenance
                "subject_dependency_role": (
                    ev.syntax_evidence[0].subject_dependency_role
                    if ev.syntax_evidence else ""
                ),
                "object_dependency_role": (
                    ev.syntax_evidence[0].object_dependency_role
                    if ev.syntax_evidence else ""
                ),
                "voice": (
                    ev.syntax_evidence[0].voice
                    if ev.syntax_evidence else ""
                ),
                "direction_source": (
                    ev.syntax_evidence[0].direction_source
                    if ev.syntax_evidence else ""
                ),
                "direction_confidence": (
                    ev.syntax_evidence[0].direction_confidence
                    if ev.syntax_evidence else ""
                ),
                # P3 confusion matrix diagnostics
                "all_predicate_scores": {
                    p.predicate: round(p.score, 4) for p in ev.predicate_scores
                },
                "reverse_scores": dict(ev.reverse_scores),
                "gold_score": (
                    next((p.score for p in ev.predicate_scores
                          if p.predicate == matched_gold["predicate"]), None)
                    if matched_gold else None
                ),
                "gold_rank": (
                    sorted(
                        (p.score for p in ev.predicate_scores), reverse=True
                    ).index(next(
                        p.score for p in ev.predicate_scores
                        if p.predicate == matched_gold["predicate"]
                    )) + 1
                    if matched_gold and any(
                        p.predicate == matched_gold["predicate"]
                        for p in ev.predicate_scores
                    ) else None
                ),
                "margin": decision.margin,
                "direction_margin": decision.direction_margin,
                "syntax_predicates": [
                    se.canonical_predicate for se in ev.syntax_evidence
                ],
            })

    # Deduplication
    _deduplicate(results)

    # Determinism hash
    master_hash = hashlib.sha256(
        "|".join(run_hashes).encode()
    ).hexdigest()[:32]
    print(f"[{mode_label}] MASTER RUN HASH: {master_hash}")
    print(f"[{mode_label}] Total pairs: {len(results)}")

    return results, suppressions


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def _deduplicate(results: list[dict]) -> None:
    """One record per canonical (subject, predicate, object) triple.

    Multiple span pairs may refer to the same canonical triple. Keep only
    the highest-scoring record. Distinct predicates on the same entity pair
    are NOT duplicates.

    Cross-sentence review pairs (is_review_cross) are excluded from dedup:
    they are held for human review and must remain visible. Deduplication
    is a production-write concern, not a review-queue concern.
    """
    _best_by_triple: dict[tuple[str, str, str], int] = {}
    _dup_count = 0
    for i, r in enumerate(results):
        if not (r["is_accepted"] or r.get("is_shadow") or r.get("is_review_unknown")):
            continue
        if r.get("is_review_cross"):
            continue  # cross-sentence review pairs are never deduplicated
        pred = r.get("gate_pred") or ""
        triple_key = (r["canonical_subject_id"], pred, r["canonical_object_id"])
        if triple_key in _best_by_triple:
            best_idx = _best_by_triple[triple_key]
            if r["score"] > results[best_idx]["score"]:
                _demote(results[best_idx])
                _best_by_triple[triple_key] = i
                _dup_count += 1
            else:
                _demote(r)
                _dup_count += 1
        else:
            _best_by_triple[triple_key] = i
    if _dup_count:
        print(f"  [dedup] {_dup_count} duplicate triples removed")


def _demote(r: dict) -> None:
    """Mark a result as deduplicated (no longer counts)."""
    r["is_accepted"] = False
    r["is_correct"] = False
    r["is_review_unknown"] = False
    r["is_review_correct"] = False
    r["is_review_cross"] = False
    r["is_cross_correct"] = False
    r["status"] = "deduplicated"


def _make_bypass_policy(policy):
    """Create a policy variant with all allowed_types constraints removed.

    Used in bypass-types diagnostic mode to show the theoretical maximum
    recall if all endpoint types were valid and specific. The type
    constraint gate (REJECT_TYPE_INVALID) never fires, and the
    REVIEW_UNKNOWN_TYPE downgrade is effectively bypassed because
    the oracle type injection provides non-unknown types.
    """
    from services.extraction.corroboration_gate import RelationPolicy, PredicatePolicy
    from dataclasses import replace as dc_replace

    # Build a new policy with allowed_types=None for every predicate
    bypass_predicates = {}
    for pred_name in policy.predicates():
        pp = policy[pred_name]
        bypass_predicates[pred_name] = {
            "standard_threshold": pp.standard_threshold,
            "standard_margin": pp.standard_margin,
            "corroborated_threshold": pp.corroborated_threshold,
            "corroborated_margin": pp.corroborated_margin,
            "direction_margin": pp.direction_margin,
            # allowed_types omitted → None → unconstrained
        }
    return RelationPolicy(policy._defaults, bypass_predicates)


# ---------------------------------------------------------------------------
# Summary and comparison reporting
# ---------------------------------------------------------------------------


def _compute_metrics(results: list[dict], all_gold: list[dict]) -> dict:
    """Compute the renamed P1A metrics from results."""
    total_gold = len(all_gold)
    all_accepted = [r for r in results if r["is_accepted"]]
    all_correct = [r for r in all_accepted if r["is_correct"]]
    all_incorrect = [r for r in all_accepted if not r["is_correct"]]
    all_shadow = [r for r in results if r.get("is_shadow")]
    all_shadow_correct = [r for r in all_shadow if r.get("is_shadow_correct")]
    all_review_unknown = [r for r in results if r.get("is_review_unknown")]
    all_review_correct = [r for r in all_review_unknown if r.get("is_review_correct")]
    all_review_cross = [r for r in results if r.get("is_review_cross")]
    all_cross_correct = [r for r in all_review_cross if r.get("is_cross_correct")]

    # Gold coverage: unique gold relations found in any output record
    # (exact-offset pair key: sid|head_start|head_end|tail_start|tail_end)
    gold_found_keys = set()
    gold_triple_keys = set()
    for r in results:
        pk = r.get("gold_pair_key")
        if pk:
            gold_found_keys.add(pk)
            if r.get("pred_match"):
                gold_triple_keys.add(r.get("gold_triple_key", pk))

    # Entity alignment failures: gold relations not found in union at all
    entity_alignment_failure = total_gold - len(gold_found_keys)

    return {
        "total_gold": total_gold,
        "total_pairs": len(results),
        "accepted": len(all_accepted),
        "accepted_correct": len(all_correct),
        "accepted_incorrect": len(all_incorrect),
        "shadow": len(all_shadow),
        "shadow_correct": len(all_shadow_correct),
        "review_unknown": len(all_review_unknown),
        "review_unknown_correct": len(all_review_correct),
        "review_cross": len(all_review_cross),
        "review_cross_correct": len(all_cross_correct),
        # --- Renamed metrics (P1A spec) ---
        "candidate_gold_coverage": len(gold_found_keys) / total_gold if total_gold else 0.0,
        "gold_triple_recall": len(gold_triple_keys) / total_gold if total_gold else 0.0,
        "accepted_gold_recall": len(all_correct) / total_gold if total_gold else 0.0,
        "accepted_labeled_hit_rate": (
            len(all_correct) / len(all_accepted) if all_accepted else 0.0
        ),
        # Shadow lane metrics (Relex-only, precision unverified)
        "shadow_gold_recall": len(all_shadow_correct) / total_gold if total_gold else 0.0,
        "shadow_labeled_hit_rate": (
            len(all_shadow_correct) / len(all_shadow) if all_shadow else 0.0
        ),
        # Combined production + shadow recall (upper bound if shadow promoted)
        "combined_recall_ceiling": (
            (len(all_correct) + len(all_shadow_correct)) / total_gold
            if total_gold else 0.0
        ),
        "review_recoverable_coverage": (
            (len(all_correct) + len(all_review_correct)) / total_gold
            if total_gold else 0.0
        ),
        "cross_sentence_review_coverage": (
            len(all_cross_correct) / total_gold if total_gold else 0.0
        ),
        "entity_alignment_failure": entity_alignment_failure,
        "entity_alignment_failure_rate": (
            entity_alignment_failure / total_gold if total_gold else 0.0
        ),
    }


def _print_summary(results: list[dict], all_gold: list[dict]) -> None:
    """Print the full summary for a single pipeline run."""
    m = _compute_metrics(results, all_gold)

    print(f"\n{'=' * 120}")
    print("UNIFIED SHADOW PIPELINE — SUMMARY")
    print(f"{'=' * 120}")

    # Per-status breakdown
    print(f"\n--- Per-Status Breakdown ---")
    status_counts: dict[str, int] = {}
    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    for status in sorted(status_counts):
        print(f"  {status:<40s} {status_counts[status]:>6d}")

    # Core metrics (renamed per P1A)
    print(f"\n--- Core Metrics (P1A) ---")
    print(f"  candidate_gold_coverage:        {m['candidate_gold_coverage']:.3f} "
          f"({int(m['candidate_gold_coverage'] * m['total_gold'])}/{m['total_gold']})")
    print(f"  gold_triple_recall:             {m['gold_triple_recall']:.3f} "
          f"({int(m['gold_triple_recall'] * m['total_gold'])}/{m['total_gold']})  [pair + correct predicate]")
    print(f"  accepted_gold_recall:           {m['accepted_gold_recall']:.3f} "
          f"({m['accepted_correct']}/{m['total_gold']})")
    print(f"  accepted_labeled_hit_rate:      {m['accepted_labeled_hit_rate']:.3f} "
          f"({m['accepted_correct']}/{m['accepted']})  [non-exhaustive gold — NOT precision]")
    print(f"  review_recoverable_coverage:    {m['review_recoverable_coverage']:.3f} "
          f"({m['accepted_correct'] + m['review_unknown_correct']}/{m['total_gold']})")
    print(f"  cross_sentence_review_coverage: {m['cross_sentence_review_coverage']:.3f} "
          f"({m['review_cross_correct']}/{m['total_gold']})")
    print(f"  entity_alignment_failure:       {m['entity_alignment_failure']}/{m['total_gold']} "
          f"({m['entity_alignment_failure_rate']:.3f})")

    # Shadow lane metrics (P1B: Relex-only, hit rate unverified)
    print(f"\n--- Shadow Lane (SHADOW_RELEX_HIGH) ---")
    print(f"  shadow_total:                   {m['shadow']}")
    print(f"  shadow_gold_recall:             {m['shadow_gold_recall']:.3f} "
          f"({m['shadow_correct']}/{m['total_gold']})")
    print(f"  shadow_labeled_hit_rate:        {m['shadow_labeled_hit_rate']:.3f} "
          f"({m['shadow_correct']}/{m['shadow']})  [non-exhaustive gold]")
    print(f"  combined_recall_ceiling:        {m['combined_recall_ceiling']:.3f} "
          f"({m['accepted_correct'] + m['shadow_correct']}/{m['total_gold']})  [if shadow promoted]")

    # Type provenance diagnostics
    print(f"\n--- Type Provenance ---")
    type_status_counts: dict[str, int] = {}
    failure_stage_counts: dict[str, int] = {}
    for r in results:
        ts = r.get("type_status", "unknown")
        type_status_counts[ts] = type_status_counts.get(ts, 0) + 1
        fs = r.get("type_failure_stage", "")
        if fs:
            failure_stage_counts[fs] = failure_stage_counts.get(fs, 0) + 1
    for ts, count in sorted(type_status_counts.items(), key=lambda x: -x[1]):
        print(f"  type_status={ts:<20s} {count:>6d}")
    if failure_stage_counts:
        print(f"  --- Failure Stages ---")
        for fs, count in sorted(failure_stage_counts.items(), key=lambda x: -x[1]):
            print(f"  {fs:<40s} {count:>6d}")

    # P4-lite cross-sentence containment diagnostics
    reject_scope = [r for r in results if r["status"] == "reject_scope"]
    review_cross = [r for r in results if r["status"] == "review_cross_sentence"]
    if reject_scope or review_cross:
        print(f"\n--- P4-lite Cross-Sentence Containment ---")
        print(f"  reject_scope:                   {len(reject_scope)}")
        print(f"  review_cross_sentence:          {len(review_cross)}")
        # Aggregate by distance
        dist_counts: dict[int, int] = {}
        for r in reject_scope + review_cross:
            d = r.get("sentence_distance", 0)
            dist_counts[d] = dist_counts.get(d, 0) + 1
        print(f"  --- By Sentence Distance ---")
        for d in sorted(dist_counts):
            print(f"    distance={d:<3d} {dist_counts[d]:>6d}")
        # Top-5 highest scoring reject_scope
        if reject_scope:
            top_rejected = sorted(reject_scope, key=lambda r: -r["score"])[:5]
            print(f"  --- Top-5 Highest Scoring REJECT_SCOPE ---")
            for r in top_rejected:
                print(f"    {r['score']:.3f}  {r['triple']}  dist={r.get('sentence_distance', '?')}")

    # Gold relation coverage
    _print_gold_coverage(results, all_gold)


def _print_comparison(
    normal_results: list[dict],
    oracle_results: list[dict],
    all_gold: list[dict],
    oracle_label: str = "ORACLE",
) -> None:
    """Print side-by-side comparison of normal vs oracle/bypass replay."""
    nm = _compute_metrics(normal_results, all_gold)
    om = _compute_metrics(oracle_results, all_gold)

    print(f"\n{'=' * 120}")
    print(f"TYPED GATE REPLAY — COMPARISON (Normal vs {oracle_label})")
    print(f"{'=' * 120}")

    print(f"\n{'Metric':<40s} {'Normal (null types)':>20s} {oracle_label + ' types)':>20s} {'Delta':>10s}")
    print(f"{'-' * 90}")

    rows = [
        ("accepted_gold_recall", nm["accepted_gold_recall"], om["accepted_gold_recall"]),
        ("accepted_labeled_hit_rate", nm["accepted_labeled_hit_rate"], om["accepted_labeled_hit_rate"]),
        ("accepted_correct", nm["accepted_correct"], om["accepted_correct"]),
        ("accepted_incorrect", nm["accepted_incorrect"], om["accepted_incorrect"]),
        ("review_unknown", nm["review_unknown"], om["review_unknown"]),
        ("review_unknown_correct", nm["review_unknown_correct"], om["review_unknown_correct"]),
        ("review_cross", nm["review_cross"], om["review_cross"]),
        ("review_cross_correct", nm["review_cross_correct"], om["review_cross_correct"]),
        ("candidate_gold_coverage", nm["candidate_gold_coverage"], om["candidate_gold_coverage"]),
        ("review_recoverable_coverage", nm["review_recoverable_coverage"], om["review_recoverable_coverage"]),
    ]
    for label, nv, ov in rows:
        delta = ov - nv
        if isinstance(nv, float):
            print(f"  {label:<38s} {nv:>20.3f} {ov:>20.3f} {delta:>+10.3f}")
        else:
            print(f"  {label:<38s} {nv:>20d} {ov:>20d} {delta:>+10d}")

    # Per-status comparison
    print(f"\n--- Per-Status Breakdown ---")
    n_status: dict[str, int] = {}
    o_status: dict[str, int] = {}
    for r in normal_results:
        n_status[r["status"]] = n_status.get(r["status"], 0) + 1
    for r in oracle_results:
        o_status[r["status"]] = o_status.get(r["status"], 0) + 1
    all_statuses = sorted(set(n_status) | set(o_status))
    print(f"  {'Status':<40s} {'Normal':>8s} {'Oracle':>8s}")
    for s in all_statuses:
        print(f"  {s:<40s} {n_status.get(s, 0):>8d} {o_status.get(s, 0):>8d}")

    # Gold coverage for oracle
    print(f"\n--- Gold Relation Coverage ({oracle_label}) ---")
    _print_gold_coverage(oracle_results, all_gold)

    # Interpretation
    print(f"\n--- Interpretation ---")
    recovered = om["accepted_correct"] - nm["accepted_correct"]
    new_fp = om["accepted_incorrect"] - nm["accepted_incorrect"]
    print(f"  Type repair recovered {recovered} correct triples "
          f"and released {new_fp} false positives.")
    print(f"  Net recall gain: {om['accepted_gold_recall'] - nm['accepted_gold_recall']:+.3f}")
    if om["accepted"] > 0:
        print(f"  Oracle accepted_labeled_hit_rate: {om['accepted_labeled_hit_rate']:.3f}")


def _print_gold_coverage(results: list[dict], all_gold: list[dict]) -> None:
    """Print per-gold-relation coverage markers."""
    print(f"\n--- Gold Relation Coverage ---")
    for gr in all_gold:
        sid = gr["sample_id"]
        gold_triple = f"{gr['head_text']} → {gr['predicate']} → {gr['tail_text']}"
        matched = [r for r in results if r["sid"] == sid and r["matched_gold"]
                   and _surface_match(r["subject"], gr["head_text"])
                   and _surface_match(r["object"], gr["tail_text"])]
        if matched:
            best = max(matched, key=lambda r: r["score"])
            if best["is_correct"]:
                marker = "✓"
            elif best.get("is_review_correct"):
                marker = "R"
            elif best.get("is_cross_correct"):
                marker = "C"
            elif best["is_accepted"]:
                marker = "A"
            elif best.get("is_review_unknown"):
                marker = "r"
            elif best.get("is_review_cross"):
                marker = "c"
            else:
                marker = "✗"
            print(f"  [{marker}] {gold_triple:<50s} "
                  f"gate={best['gate_pred'] or '?':<16s} "
                  f"status={best['status']}")
        else:
            print(f"  [?] {gold_triple:<50s} NOT FOUND IN UNION")


if __name__ == "__main__":
    main()

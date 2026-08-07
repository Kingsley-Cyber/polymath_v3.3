"""Shared evidence contract for the relation corroboration gate.

This is the common data shape exchanged between:
  - Relex raw pair scores (per-predicate sigmoid, both directions)
  - spaCy dependency-path evidence (DependencyMatcher patterns)
  - SVO candidate evidence (native textacy-equivalent extractor)

The join key between evidence sources is the STABLE SPAN OFFSET pair:
    (chunk_id, subject_start, subject_end, object_start, object_end)

Do NOT join on fuzzy entity strings — surface variants ("cold lead" vs
"cold leads") must be resolved via mention_normalizer before producing
evidence records. The gate consumes already-resolved evidence.

SOURCE FAMILIES
    Syntax evidence is grouped into source families. Two extractors that
draw from the SAME underlying parse are NOT independent confirmations.
    DependencyMatcher and native SVO both run over the spaCy dependency parse,
so they share source_family `spacy_dependency_parse`. Their agreement is
weaker evidence than agreement across two independent parses.

JOIN MODES
    Every joined SyntaxEvidence carries a join_mode describing HOW it was
matched to the Relex pair. Trust values are deterministic:
        exact_span        trust 1.00  (full-trust, production)
        canonical_entity  trust 0.80  (high-trust, same entity different span)
        resolved_alias    trust 0.70  (medium-high, alias resolution matched)
        normalized_surface trust 0.30 (LOW trust — shadow-only)
    `normalized_surface` joins CANNOT independently authorize a production
graph write. They are kept for diagnostic visibility only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Sequence


# ---------------------------------------------------------------------------
# Score-level contracts
# ---------------------------------------------------------------------------


class JoinMode(StrEnum):
    """How a SyntaxEvidence record was matched to a Relex pair.

    Trust values are defined in JOIN_TRUST. Modes in SHADOW_ONLY_JOIN_MODES
    cannot authorize a production graph write and cannot independently
    trigger REVIEW_CONFLICT.
    """

    EXACT_SPAN = "exact_span"
    CANONICAL_ENTITY = "canonical_entity"
    RESOLVED_ALIAS = "resolved_alias"
    HEAD_TOKEN = "head_token"  # true head-token containment (P2A)
    SPAN_CONTAINMENT = "span_containment"  # phrase-span containment (P2A)
    NORMALIZED_SURFACE = "normalized_surface"


class TypeStatus(StrEnum):
    """Provenance status of an endpoint's effective type.

    AGREED         — relex and canonical registry concur
    RELEX_ONLY     — only the Relex model emitted a type
    RULE_ONLY      — only a rule-based resolver produced a type
    CANONICAL_ONLY — only the canonical registry had a type
    ORACLE         — type injected from gold (evaluation only, never production)
    OTHER          — type is the literal "other" (entity exists, type unknown)
    UNKNOWN        — no source produced a type
    CONFLICT       — relex and canonical disagree
    """

    AGREED = "agreed"
    RELEX_ONLY = "relex_only"
    RULE_ONLY = "rule_only"
    CANONICAL_ONLY = "canonical_only"
    ORACLE = "oracle"
    OTHER = "other"
    UNKNOWN = "unknown"
    CONFLICT = "conflict"


class TypeFailureStage(StrEnum):
    """Deterministic stage where an endpoint type was lost.

    Used for diagnostic attribution so the gate can report WHERE a type
    disappeared rather than merely reporting "unknown".
    """

    RAW_MODEL_TYPE_MISSING = "raw_model_type_missing"
    RELATION_ENTITY_INDEX_INVALID = "relation_entity_index_invalid"
    RELATION_TO_ENTITY_JOIN_FAILED = "relation_to_entity_join_failed"
    TYPE_NORMALIZATION_FAILED = "type_normalization_failed"
    TYPE_DROPPED_DURING_CONSOLIDATION = "type_dropped_during_consolidation"
    CANONICAL_TYPE_CONFLICT = "canonical_type_conflict"
    TYPE_DROPPED_DURING_SERIALIZATION = "type_dropped_during_serialization"


class CrossSentenceLinkType(StrEnum):
    """Typed classification of cross-sentence discourse links (P4-lite hardening).

    Replaces the boolean has_cross_sentence_link flag with a typed signal so
    the gate can enforce per-predicate allowed_links. Only approved link types
    authorize REVIEW_CROSS_SENTENCE; all others fall through to REJECT_SCOPE.
    """

    EXACT_ENTITY_REPEAT = "exact_entity_repeat"
    RESOLVED_ALIAS = "resolved_alias"
    VALIDATED_COREFERENCE = "validated_coreference"
    HEADING_CONTINUATION = "heading_continuation"
    LIST_CONTINUATION = "list_continuation"
    DOCUMENT_STRUCTURE_LINK = "document_structure_link"
    NONE = "none"


# Deterministic trust per join mode. The gate uses these to weight conflicts
# and corroboration. Higher = more trusted.
JOIN_TRUST: dict[JoinMode, float] = {
    JoinMode.EXACT_SPAN: 1.00,
    JoinMode.CANONICAL_ENTITY: 0.80,
    JoinMode.RESOLVED_ALIAS: 0.70,
    JoinMode.HEAD_TOKEN: 0.60,
    JoinMode.SPAN_CONTAINMENT: 0.50,
    JoinMode.NORMALIZED_SURFACE: 0.30,
}

# Modes that are diagnostic-only. They attach syntax evidence for visibility
# but cannot independently authorize production writes, satisfy corroboration,
# or trigger conflicts. A trusted structural source (HEAD_TOKEN, EXACT_SPAN,
# CANONICAL_ENTITY, RESOLVED_ALIAS) is required for ACCEPT_CORROBORATED.
SHADOW_ONLY_JOIN_MODES: frozenset[JoinMode] = frozenset({
    JoinMode.NORMALIZED_SURFACE,
    JoinMode.SPAN_CONTAINMENT,
})

# The minimum trust required for a syntax record to independently trigger
# REVIEW_CONFLICT or satisfy corroboration for ACCEPT_CORROBORATED.
# Below this threshold, evidence is diagnostic only.
CONFLICT_TRUST_FLOOR: float = 0.50


# Source families. Two syntax records drawn from the same source family are
# NOT independent confirmations — they are views on the same underlying parse.
SOURCE_FAMILY_SPACY_DEP = "spacy_dependency_parse"


@dataclass(frozen=True)
class PredicateScore:
    """One predicate label with its sigmoid score for an ordered pair."""

    predicate: str
    score: float


# ---------------------------------------------------------------------------
# Syntax evidence contracts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SyntaxEvidence:
    """Deterministic syntactic evidence for one (subject, object) pair.

    Produced by DependencyMatcher patterns or the native SVO extractor.
    The canonical_predicate MUST already be resolved through the existing
    T1-T4 resolver (dep_path_extractor.resolve_predicate).

    source_family: groups evidence that derives from the same parse.
        DependencyMatcher and native SVO both consume the spaCy dependency
        parse, so they share `spacy_dependency_parse` and are NOT
        independent confirmations.

    join_mode: how this evidence was matched to the Relex pair. See
        JoinMode and JOIN_TRUST for the trust contract.
    """

    canonical_predicate: str
    surface_predicate: str
    pattern_id: str
    confidence: float
    negated: bool = False
    modal: bool = False
    # Default source_family is the spaCy dependency parse — both
    # DependencyMatcher and native SVO draw from it. Other extractors
    # (e.g. a future constituency parser) would set their own family.
    source_family: str = SOURCE_FAMILY_SPACY_DEP
    # Default join_mode is exact_span — the highest-trust mode. Adapters
    # that join via canonical_entity / resolved_alias / normalized_surface
    # set this explicitly so the gate can weight the evidence correctly.
    join_mode: str = JoinMode.EXACT_SPAN.value
    # Argument-role and direction provenance (open-relation routing).
    subject_dependency_role: str = ""   # agent | patient | theme | possessor | anchor
    object_dependency_role: str = ""    # agent | patient | theme | possessed | appositive
    voice: str = ""                     # active | passive | nominal
    direction_source: str = ""          # DEPENDENCY_FRAME | RESOLVER_SWAP | NOMINAL
    direction_confidence: str = ""      # high | medium | low

    @property
    def join_trust(self) -> float:
        """Trust weight for this join mode (gate uses this, not raw join_mode)."""
        try:
            mode = JoinMode(self.join_mode)
        except ValueError:
            return 0.0
        return float(JOIN_TRUST.get(mode, 0.0))


# ---------------------------------------------------------------------------
# Combined evidence for one ordered pair
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelationEvidence:
    """All evidence for one ordered (subject, object) pair in one chunk.

    predicate_scores: Relex raw sigmoid scores for this direction, ALL labels
    (not just above threshold). The gate needs scores below threshold to
    detect under-confident-but-correctly-ranked predicates.

    reverse_scores: Relex sigmoid scores for the SAME predicate labels on the
    reversed (object, subject) pair. Used to compute direction_margin.

    syntax_evidence: zero or more syntactic matches (dep-path, SVO) for this
    ordered pair. Empty tuple when no syntax evidence fires.

    Type provenance fields (P1A):
    raw_relex_type / raw_relex_type_score: the model's original entity label
    effective_type: the resolved type used by the gate
    type_status: provenance of the effective type (see TypeStatus)
    type_failure_stage: when type is unknown, WHERE it was lost
    """

    chunk_id: str

    subject_id: str
    subject_text: str
    subject_type: str
    subject_start: int
    subject_end: int

    object_id: str
    object_text: str
    object_type: str
    object_start: int
    object_end: int

    predicate_scores: Sequence[PredicateScore]
    reverse_scores: Mapping[str, float]
    syntax_evidence: Sequence[SyntaxEvidence] = field(default_factory=tuple)
    # Pair-scope flag: True when both endpoints occur in the same sentence.
    # Cross-sentence pairs without syntax evidence are rejected by the gate.
    same_sentence: bool = True
    # Sentence distance: number of sentence boundaries between the two spans.
    # 0 = same sentence, 1 = adjacent, 2+ = non-adjacent. Used by P4-lite
    # cross-sentence containment to reject distant pairs outright.
    sentence_distance: int = 0
    # Whether an explicit cross-sentence link exists (repeated entity, alias,
    # heading continuation). Required for adjacent-sentence pairs to qualify
    # for REVIEW rather than REJECT_SCOPE.
    has_cross_sentence_link: bool = False
    # P4-lite hardening: typed link classification. Prevents the boolean flag
    # from becoming an overly broad escape hatch. Per-predicate allowed_links
    # in the gate determine which link types authorize REVIEW_CROSS_SENTENCE.
    cross_sentence_link_type: str = "NONE"

    # --- Type provenance (P1A) ---
    raw_relex_type: str = ""  # model's original entity label (pre-normalization)
    raw_relex_type_score: float = 0.0  # model's entity detection confidence
    effective_type_status: str = TypeStatus.UNKNOWN.value  # subject endpoint
    object_type_status: str = TypeStatus.UNKNOWN.value  # object endpoint
    type_failure_stage: str = ""  # where the type was lost (diagnostic)

    @property
    def relation_key(self) -> tuple[str, int, int, int, int]:
        """Stable span-based join key. Use this to align evidence sources."""
        return (
            self.chunk_id,
            self.subject_start,
            self.subject_end,
            self.object_start,
            self.object_end,
        )

    @property
    def canonical_subject_id(self) -> str:
        """Canonical graph entity ID for the subject, computed at extraction time.

        This lets evidence from different mention positions (same entity at
        different char offsets) join on identity rather than fragile span keys.
        Computed lazily from the canonical module — the single source of truth.
        """
        from services.extraction.canonical import entity_id_from_name
        return entity_id_from_name(self.subject_text)

    @property
    def canonical_object_id(self) -> str:
        """Canonical graph entity ID for the object."""
        from services.extraction.canonical import entity_id_from_name
        return entity_id_from_name(self.object_text)


# ---------------------------------------------------------------------------
# Gate decision contracts
# ---------------------------------------------------------------------------


class GateStatus(StrEnum):
    """Deterministic decision states emitted by the corroboration gate.

    STORE_UNMAPPED_SURFACE_RELATION fires when syntax produced a
    grammatically licensed subject-predicate-object relation, the surface
    predicate is meaningful, the relation is not negated/hypothetical/
    conditional, but no canonical ontology predicate adequately maps to it.
    It does NOT fire for weak Relex scores, canonical conflicts, or mere
    co-presence. These records go to a review/evidence collection (NOT
    Neo4j) for later ontology-extension analysis.

    REVIEW_UNKNOWN_TYPE fires when a pair would otherwise be accepted
    (ACCEPT_HIGH or ACCEPT_CORROBORATED) but one or both endpoint types
    are unresolved ("unknown").  The pair is held for review rather than
    receiving normal high-confidence acceptance.  It is NOT a rejection.

    REVIEW_CROSS_SENTENCE fires when a cross-sentence pair lacks trusted
    syntax evidence.  A high Relex score alone does NOT authorize
    cross-sentence acceptance — the score indicates predicate fit for an
    entity pair, not that the text asserts the relation across sentence
    boundaries.  These pairs are held for review until a separately
    annotated cross-sentence benchmark exists.
    """

    ACCEPT_HIGH = "accept_high"
    ACCEPT_CORROBORATED = "accept_corroborated"
    ACCEPT_SYNTAX_HIGH = "accept_syntax_high"  # trusted syntax-only, all checks pass
    SHADOW_RELEX_HIGH = "shadow_relex_high"  # Relex-only, precision unverified
    STORE_UNMAPPED_SURFACE_RELATION = "store_unmapped_surface_relation"
    REVIEW_UNMAPPED_RELATION = "review_unmapped_relation"  # direction ambiguous
    REVIEW_CONFLICT = "review_conflict"
    REVIEW_UNKNOWN_TYPE = "review_unknown_type"
    REVIEW_CROSS_SENTENCE = "review_cross_sentence"
    REVIEW_TYPE_CONFLICT = "review_type_conflict"
    REVIEW_TYPE_COMPATIBILITY = "review_type_compatibility"
    REVIEW_SYNTAX_ONLY = "review_syntax_only"  # syntax-only, insufficient for acceptance
    REJECT_SELF_LOOP = "reject_self_loop"
    REJECT_ENDPOINT_SIGNATURE = "reject_endpoint_signature"
    REJECT_TYPE_INVALID = "reject_type_invalid"  # legacy alias
    REJECT_NEGATED = "reject_negated"
    REJECT_LOW_EVIDENCE = "reject_low_evidence"
    REJECT_SCOPE = "reject_scope"


@dataclass(frozen=True)
class GateDecision:
    """Immutable decision for one ordered pair.

    predicate is None only when there are no predicate scores at all.
    score/margin/direction_margin are the raw sigmoid relation scores for
    the top-1 predicate. These are NOT calibrated probabilities — they are
    model scores on the sigmoid scale, suitable for ranking and thresholding.

    logit_margin / logit_direction_margin are the same margins computed in
    logit space. The sigmoid compresses high and low values nonlinearly, so
    logit-space margins reveal evidentiary separation that raw sigmoid
    margins hide. These are DIAGNOSTIC ONLY — threshold gates operate on
    sigmoid-space values until a calibration set justifies logit-space gates.

    blocking_reasons preserves ALL blockers (not just the primary one).
    The primary decision (status) remains deterministic per gate order,
    while blocking_reasons gives complete diagnostics.
    """

    status: GateStatus
    predicate: str | None
    score: float
    margin: float
    direction_margin: float
    reasons: tuple[str, ...]
    # Logit-space diagnostic margins (analysis only, not used by thresholds).
    logit_margin: float = 0.0
    logit_direction_margin: float = 0.0
    # Complete blocker list (all conditions that would block acceptance).
    blocking_reasons: tuple[str, ...] = ()

"""Deterministic corroboration gate between Relex raw scores and syntax evidence.

This module is the BRIDGE between two already-successful pipelines:
  1. Relex N² pair scoring (per-predicate sigmoid)
  2. spaCy DependencyMatcher + native SVO extraction

Neither pipeline is modified. The gate consumes their outputs through the
shared RelationEvidence contract and emits a GateDecision per ordered pair.

Decision states (see GateStatus):
  ACCEPT_HIGH          — Relex clears the standard bar alone
  ACCEPT_CORROBORATED  — Relex below standard but above corroborated threshold,
                         AND syntax evidence agrees on the predicate
  ACCEPT_SYNTAX_HIGH   — Trusted syntax-only with unambiguous canonical mapping,
                         valid endpoint types, valid direction, no self-loop
  REVIEW_CONFLICT      — Relex top-1 disagrees with deterministic syntax
  REVIEW_SYNTAX_ONLY   — Syntax-only but insufficient for acceptance
  REJECT_SELF_LOOP     — Subject and object resolve to same canonical entity
  REJECT_TYPE_INVALID  — endpoint types not in allowed_types
  REJECT_NEGATED       — syntax evidence contains a negation marker
  REJECT_LOW_EVIDENCE  — insufficient combined evidence

Join key: stable span offsets (chunk_id, subject_start/end, object_start/end).
Never join on fuzzy entity strings.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import yaml

from services.extraction.relation_evidence import (
    CONFLICT_TRUST_FLOOR,
    JOIN_TRUST,
    CrossSentenceLinkType,
    GateDecision,
    GateStatus,
    JoinMode,
    RelationEvidence,
    SHADOW_ONLY_JOIN_MODES,
    SyntaxEvidence,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Direction-trust routing for open (unmapped) relations.
#
# STORE_UNMAPPED_SURFACE_RELATION requires ALL of:
#   1. direction_confidence == "high"
#   2. Both argument slots populated (non-empty roles)
#   3. Role ordering is an approved construction signature
#   4. Distinct resolved mentions (checked upstream by self-loop guard)
#
# Approved role orderings: these are the construction families where the
# FrameExtractor's argument assignment is structurally validated.
# ---------------------------------------------------------------------------

_APPROVED_ROLE_ORDERINGS: frozenset[tuple[str, str]] = frozenset({
    ("agent", "patient"),           # active transitive: X does Y
    ("agent", "theme"),             # X acts on Y
    ("agent", "co_participant"),    # X works with Y
    ("possessor", "possessed"),     # X's Y
    ("anchor", "appositive"),       # X, a Y
    ("container", "content"),       # X contains Y
    ("whole", "part"),              # X has part Y
    ("source", "destination"),      # X to Y
    ("cause", "effect"),            # X causes Y
    ("experiencer", "stimulus"),    # X experiences Y
    ("experiencer", "source"),      # X benefits from Y
})


def _direction_is_trusted(se: "SyntaxEvidence") -> bool:
    """True when an unmapped syntax record has trusted argument assignment.

    Requires high confidence, complete slots, and an approved role ordering.
    """
    if se.direction_confidence != "high":
        return False
    subj_role = se.subject_dependency_role
    obj_role = se.object_dependency_role
    if not subj_role or not obj_role:
        return False  # incomplete argument slots
    return (subj_role, obj_role) in _APPROVED_ROLE_ORDERINGS


# ---------------------------------------------------------------------------
# P4-lite hardening: per-predicate allowed cross-sentence link types.
#
# Only these link types authorize REVIEW_CROSS_SENTENCE for the given
# predicate. Predicates not listed here fall back to the DEFAULT set.
# This prevents the boolean has_cross_sentence_link flag from becoming
# an overly broad escape hatch.
#
# IMPLEMENTED_LINK_TYPES: only these are actually detected by the pipeline.
# Configured but unimplemented types MUST NOT authorize review.
# ---------------------------------------------------------------------------

IMPLEMENTED_LINK_TYPES: frozenset[str] = frozenset({
    CrossSentenceLinkType.EXACT_ENTITY_REPEAT,
})

# Declared but NOT yet detected. These are reserved for future
# implementation and MUST NOT authorize review until a detector exists.
_DISABLED_LINK_TYPES: frozenset[str] = frozenset({
    CrossSentenceLinkType.RESOLVED_ALIAS,
    CrossSentenceLinkType.VALIDATED_COREFERENCE,
    CrossSentenceLinkType.HEADING_CONTINUATION,
    CrossSentenceLinkType.LIST_CONTINUATION,
    CrossSentenceLinkType.DOCUMENT_STRUCTURE_LINK,
})

_DEFAULT_ALLOWED_LINKS: frozenset[str] = frozenset({
    CrossSentenceLinkType.EXACT_ENTITY_REPEAT,
    CrossSentenceLinkType.RESOLVED_ALIAS,
    CrossSentenceLinkType.VALIDATED_COREFERENCE,
})

PREDICATE_ALLOWED_LINKS: dict[str, frozenset[str]] = {
    "located_in": frozenset({
        CrossSentenceLinkType.EXACT_ENTITY_REPEAT,
        CrossSentenceLinkType.RESOLVED_ALIAS,
        CrossSentenceLinkType.VALIDATED_COREFERENCE,
    }),
    "created_by": frozenset({
        CrossSentenceLinkType.HEADING_CONTINUATION,
        CrossSentenceLinkType.LIST_CONTINUATION,
        CrossSentenceLinkType.EXACT_ENTITY_REPEAT,
    }),
    "works_for": frozenset({
        CrossSentenceLinkType.EXACT_ENTITY_REPEAT,
        CrossSentenceLinkType.RESOLVED_ALIAS,
        CrossSentenceLinkType.VALIDATED_COREFERENCE,
    }),
    "member_of": frozenset({
        CrossSentenceLinkType.EXACT_ENTITY_REPEAT,
        CrossSentenceLinkType.RESOLVED_ALIAS,
        CrossSentenceLinkType.LIST_CONTINUATION,
    }),
    "part_of": frozenset({
        CrossSentenceLinkType.EXACT_ENTITY_REPEAT,
        CrossSentenceLinkType.RESOLVED_ALIAS,
        CrossSentenceLinkType.DOCUMENT_STRUCTURE_LINK,
    }),
    "owns": frozenset({
        CrossSentenceLinkType.EXACT_ENTITY_REPEAT,
        CrossSentenceLinkType.RESOLVED_ALIAS,
        CrossSentenceLinkType.VALIDATED_COREFERENCE,
    }),
    "uses": frozenset({
        CrossSentenceLinkType.EXACT_ENTITY_REPEAT,
        CrossSentenceLinkType.RESOLVED_ALIAS,
        CrossSentenceLinkType.VALIDATED_COREFERENCE,
    }),
}


def _allowed_links_for_predicate(predicate: str) -> frozenset[str]:
    """Return the set of link types that authorize REVIEW for this predicate.

    The result is always intersected with IMPLEMENTED_LINK_TYPES so that
    a configured but unimplemented signal can never silently authorize
    review. Only detectors that actually exist in the pipeline can fire.
    """
    configured = PREDICATE_ALLOWED_LINKS.get(predicate, _DEFAULT_ALLOWED_LINKS)
    return configured & IMPLEMENTED_LINK_TYPES

# Default config path — resolve repo config/ fail-loud for BOTH host layout
# (<repo>/backend/services/extraction/) and container layout
# (/app/services/extraction/), mirroring dep_path_extractor's resolver.
def _default_config_path() -> Path:
    _this = Path(__file__).resolve()
    for parent_idx in (3, 2):
        candidate = _this.parents[parent_idx] / "config" / "relation_acceptance.yaml"
        if candidate.exists():
            return candidate
    raise RuntimeError(
        f"FATAL: config/relation_acceptance.yaml not found relative to {__file__}. "
        "Refusing to gate relations with uncertain identity."
    )


def _probability_to_logit(p: float, eps: float = 1e-7) -> float:
    """Convert a sigmoid relation score to logit space.

    The sigmoid compresses high and low values nonlinearly. Two scores
    like 0.28 and 0.01 have a raw margin of 0.27, but in logit space the
    separation is much larger because 0.01 maps to a strongly negative
    logit while 0.28 maps to a moderately negative one.

    Values are clipped to [eps, 1-eps] to avoid log(0) = -inf.
    """
    p = min(max(p, eps), 1.0 - eps)
    return math.log(p / (1.0 - p))


# ---------------------------------------------------------------------------
# Per-predicate policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PredicatePolicy:
    """Acceptance thresholds + type constraints for one predicate.

    Type constraints use three compatibility levels:
      VALID     — both endpoints in the valid sets → continue through scoring
      AMBIGUOUS — one or both endpoints in the ambiguous sets → REVIEW
      INVALID   — endpoints not in valid or ambiguous → REJECT

    Unknown/empty types bypass the gate entirely (REVIEW_UNKNOWN_TYPE is
    handled upstream).
    """

    standard_threshold: float
    standard_margin: float
    corroborated_threshold: float
    corroborated_margin: float
    direction_margin: float
    subject_types: frozenset[str] | None  # valid subject types
    object_types: frozenset[str] | None   # valid object types
    ambiguous_subject_types: frozenset[str] | None = None
    ambiguous_object_types: frozenset[str] | None = None
    allows_reflexive: bool = False  # True only for predicates that are valid self-relations

    def type_compatibility(self, head_type: str, tail_type: str) -> str:
        """Return 'valid', 'ambiguous', or 'invalid' for this endpoint pair.

        Empty/unknown types return 'valid' (bypass — handled upstream as
        REVIEW_UNKNOWN_TYPE). The literal 'other' is treated as ambiguous.
        """
        if self.subject_types is None and self.object_types is None:
            return "valid"
        if not head_type or not tail_type:
            return "valid"  # missing type info — don't reject on type alone
        if head_type in ("unknown",) or tail_type in ("unknown",):
            return "valid"  # unresolved at the adapter boundary

        ht = head_type.lower()
        tt = tail_type.lower()

        # Check valid zone first
        head_valid = (self.subject_types is None or ht in self.subject_types)
        tail_valid = (self.object_types is None or tt in self.object_types)
        if head_valid and tail_valid:
            return "valid"

        # Check ambiguous zone
        head_ambiguous = (
            head_valid
            or ht == "other"
            or (self.ambiguous_subject_types is not None and ht in self.ambiguous_subject_types)
        )
        tail_ambiguous = (
            tail_valid
            or tt == "other"
            or (self.ambiguous_object_types is not None and tt in self.ambiguous_object_types)
        )
        if head_ambiguous and tail_ambiguous:
            return "ambiguous"

        return "invalid"

    def allows_types(self, head_type: str, tail_type: str) -> bool:
        """Backward-compatible: True if not INVALID."""
        return self.type_compatibility(head_type, tail_type) != "invalid"


# ---------------------------------------------------------------------------
# Policy loader
# ---------------------------------------------------------------------------


class RelationPolicy:
    """Loaded acceptance policy: defaults + per-predicate overrides."""

    def __init__(
        self,
        defaults: dict[str, float],
        predicates: dict[str, dict[str, Any]],
    ):
        self._defaults = defaults
        self._predicates: dict[str, PredicatePolicy] = {}
        for name, spec in predicates.items():
            self._predicates[name] = self._build_policy(spec)

    def _build_policy(self, spec: dict[str, Any]) -> PredicatePolicy:
        """Merge a predicate spec over the defaults."""

        def pick(key: str) -> float:
            return float(spec.get(key, self._defaults.get(key, 0.0)))

        # V2 format: valid/ambiguous nested dicts.
        valid_block = spec.get("valid")
        ambiguous_block = spec.get("ambiguous")

        if valid_block is not None:
            raw_subject = valid_block.get("subject")
            raw_object = valid_block.get("object")
            subject = frozenset(str(t).lower() for t in raw_subject) if raw_subject else None
            object_ = frozenset(str(t).lower() for t in raw_object) if raw_object else None
        else:
            # V1 fallback: flat subject_types / object_types lists.
            raw_subject = spec.get("subject_types")
            raw_object = spec.get("object_types")
            subject = frozenset(str(t).lower() for t in raw_subject) if raw_subject else None
            object_ = frozenset(str(t).lower() for t in raw_object) if raw_object else None

        # Ambiguous zone
        amb_subject = None
        amb_object = None
        if ambiguous_block is not None:
            raw_amb_s = ambiguous_block.get("subject")
            raw_amb_o = ambiguous_block.get("object")
            amb_subject = frozenset(str(t).lower() for t in raw_amb_s) if raw_amb_s else None
            amb_object = frozenset(str(t).lower() for t in raw_amb_o) if raw_amb_o else None

        # Legacy: allowed_types as explicit (subject, object) pairs.
        if subject is None and object_ is None:
            raw_pairs = spec.get("allowed_types")
            if raw_pairs is not None:
                subject = frozenset(str(h).lower() for h, _ in raw_pairs)
                object_ = frozenset(str(t).lower() for _, t in raw_pairs)

        return PredicatePolicy(
            standard_threshold=pick("standard_threshold"),
            standard_margin=pick("standard_margin"),
            corroborated_threshold=pick("corroborated_threshold"),
            corroborated_margin=pick("corroborated_margin"),
            direction_margin=pick("direction_margin"),
            subject_types=subject,
            object_types=object_,
            ambiguous_subject_types=amb_subject,
            ambiguous_object_types=amb_object,
        )

    def __contains__(self, predicate: str) -> bool:
        return predicate in self._predicates

    def __getitem__(self, predicate: str) -> PredicatePolicy:
        if predicate not in self._predicates:
            # Predicate not in policy file — use defaults with no type gate.
            return self._build_policy({})
        return self._predicates[predicate]

    def predicates(self) -> Sequence[str]:
        return tuple(self._predicates.keys())


_policy_cache: RelationPolicy | None = None


def load_policy(config_path: str | Path | None = None) -> RelationPolicy:
    """Load and cache the acceptance policy from YAML."""
    global _policy_cache
    if _policy_cache is not None and config_path is None:
        return _policy_cache

    path = Path(config_path) if config_path else _default_config_path()
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    defaults = data.get("defaults", {})
    predicates = data.get("predicates", {})
    policy = RelationPolicy(defaults, predicates)
    if config_path is None:
        _policy_cache = policy
    return policy


def reset_policy_cache() -> None:
    """Clear the cached policy (tests use fresh configs)."""
    global _policy_cache
    _policy_cache = None


# ---------------------------------------------------------------------------
# Gate evaluation
# ---------------------------------------------------------------------------

# Reverse-score cap for syntax-assisted direction override.
# When trusted syntax explicitly determines direction, a negative
# direction_margin is tolerated only if the reverse score is below this cap.
_SYNTAX_DIRECTION_REVERSE_CAP = 0.25


def _has_unknown_types(evidence: RelationEvidence) -> bool:
    """True when either endpoint type is unresolved."""
    return (
        evidence.subject_type == "unknown"
        or evidence.object_type == "unknown"
    )


def _acceptance_status(
    base_status: GateStatus,
    evidence: RelationEvidence,
    predicate_policy: PredicatePolicy,
) -> GateStatus:
    """Downgrade acceptance to REVIEW_UNKNOWN_TYPE when endpoint types
    are unresolved.  The pair passes all score/margin/direction gates
    but is held for review rather than receiving normal high-confidence
    acceptance.  This is NOT a rejection."""
    if _has_unknown_types(evidence):
        return GateStatus.REVIEW_UNKNOWN_TYPE
    return base_status


def _direction_is_valid(
    direction_margin: float,
    predicate_policy: PredicatePolicy,
    evidence: RelationEvidence,
) -> bool:
    """Check direction validity with syntax-assisted override.

    Standard rule: direction_margin >= policy threshold.
    Override: trusted syntax that explicitly determines direction may
    tolerate a negative direction_margin, provided the reverse score
    is below a cap.  This prevents reversing e.g.
    ``book → created_by → author`` into ``author → created_by → book``
    while allowing syntax-licensed direction corrections.
    """
    if direction_margin >= predicate_policy.direction_margin:
        return True
    # Syntax-assisted override
    trusted_syntax = [
        se for se in evidence.syntax_evidence
        if se.join_trust >= JOIN_TRUST[JoinMode.CANONICAL_ENTITY]
        and se.pattern_id in ("NATIVE_SVO", "NATIVE_PASSIVE", "NATIVE_COPULA")
    ]
    if trusted_syntax:
        reverse_scores = evidence.reverse_scores
        max_reverse = max(reverse_scores.values()) if reverse_scores else 0.0
        if max_reverse < _SYNTAX_DIRECTION_REVERSE_CAP:
            return True
    return False


def evaluate_relation(
    evidence: RelationEvidence,
    policy: RelationPolicy,
) -> GateDecision:
    """Evaluate one ordered pair through the corroboration gate.

    The evaluation is deterministic: given the same evidence and policy, the
    same GateDecision is always returned. Tie-breaking in the ranked scores is
    by predicate name (alphabetical) so re-runs produce identical output.
    """
    ranked = sorted(
        evidence.predicate_scores,
        key=lambda item: (-item.score, item.predicate),
    )

    # Pre-compute negated syntax items so the negation gate can fire on the
    # syntax-only path too (where there is no `top` to populate the decision).
    negated_items = [
        item for item in evidence.syntax_evidence if item.negated
    ]

    # --- Self-loop hard reject ---
    # A relation where subject and object resolve to the same canonical entity
    # is a structural artifact (canonicalization collision or mention expansion
    # error), not a genuine relation.  Reject before any acceptance path.
    if (
        evidence.canonical_subject_id
        and evidence.canonical_subject_id == evidence.canonical_object_id
    ):
        # Check if the top predicate (if any) explicitly allows reflexive use.
        top_pred = ranked[0].predicate if ranked else None
        top_policy = policy[top_pred] if top_pred else None
        if top_policy is None or not top_policy.allows_reflexive:
            return GateDecision(
                status=GateStatus.REJECT_SELF_LOOP,
                predicate=top_pred,
                score=ranked[0].score if ranked else 0.0,
                margin=0.0,
                direction_margin=0.0,
                reasons=(
                    "self_loop_canonical_id_collision",
                    evidence.canonical_subject_id,
                ),
                blocking_reasons=("SELF_LOOP",),
            )

    # --- Syntax-only entry: no Relex predicate scores ---
    #
    # FrameExtractor triples for pairs that Relex did not preserve arrive
    # here. Possible outcomes:
    #   ACCEPT_SYNTAX_HIGH — trusted syntax, canonical mapping, valid types/direction
    #   REVIEW_SYNTAX_ONLY — syntax-only but insufficient for acceptance
    #   STORE_UNMAPPED_SURFACE_RELATION — syntax licensed a relation but no
    #     canonical ontology predicate adequately maps to the surface verb.
    if not ranked:
        return _evaluate_syntax_only(evidence, negated_items, policy)

    top = ranked[0]
    second_score = ranked[1].score if len(ranked) > 1 else 0.0
    margin = top.score - second_score

    reverse_score = evidence.reverse_scores.get(top.predicate, 0.0)
    direction_margin = top.score - reverse_score

    # Logit-space margins (diagnostic only — sigmoid compresses nonlinearly).
    top_logit = _probability_to_logit(top.score)
    second_logit = _probability_to_logit(second_score)
    reverse_logit = _probability_to_logit(reverse_score)
    logit_margin = top_logit - second_logit
    logit_direction_margin = top_logit - reverse_logit

    predicate_policy = policy[top.predicate]

    # --- Type constraint gate (P3 endpoint signatures, 3-level) ---
    compat = predicate_policy.type_compatibility(
        evidence.subject_type,
        evidence.object_type,
    )
    if compat == "invalid":
        return GateDecision(
            status=GateStatus.REJECT_ENDPOINT_SIGNATURE,
            predicate=top.predicate,
            score=top.score,
            margin=margin,
            direction_margin=direction_margin,
            logit_margin=logit_margin,
            logit_direction_margin=logit_direction_margin,
            reasons=(
                "endpoint_signature_violation",
                f"subject={evidence.subject_type}",
                f"object={evidence.object_type}",
            ),
            blocking_reasons=("ENDPOINT_SIGNATURE_INVALID",),
        )
    if compat == "ambiguous":
        return GateDecision(
            status=GateStatus.REVIEW_TYPE_COMPATIBILITY,
            predicate=top.predicate,
            score=top.score,
            margin=margin,
            direction_margin=direction_margin,
            logit_margin=logit_margin,
            logit_direction_margin=logit_direction_margin,
            reasons=(
                "endpoint_type_ambiguous",
                f"subject={evidence.subject_type}",
                f"object={evidence.object_type}",
            ),
            blocking_reasons=("ENDPOINT_TYPE_AMBIGUOUS",),
        )

    # --- Negation gate ---
    if negated_items:
        return GateDecision(
            status=GateStatus.REJECT_NEGATED,
            predicate=top.predicate,
            score=top.score,
            margin=margin,
            direction_margin=direction_margin,
            logit_margin=logit_margin,
            logit_direction_margin=logit_direction_margin,
            reasons=("negated_relation_evidence", negated_items[0].pattern_id),
            blocking_reasons=("NEGATED_RELATION",),
        )

    # --- Pair-scope gate (P4-lite cross-sentence containment) ---
    #
    # Cross-sentence pairs without trusted syntax evidence are contained:
    #   ADJACENT (distance=1) + explicit link → REVIEW_CROSS_SENTENCE
    #   ADJACENT without explicit link        → REJECT_SCOPE
    #   DISTANT (distance≥2)                  → REJECT_SCOPE
    #
    # RESOLVED syntax evidence (non-empty canonical predicate) implies a
    # grammatical link between the mentions, which validates the pair scope
    # even across sentence boundaries. UNMAPPED syntax records (empty
    # predicate) do NOT bypass this gate — they lack a named canonical
    # claim and cannot validate cross-sentence scope.
    #
    # Explicit cross-sentence links (P4-lite minimum set):
    #   - exact repeated entity (surface appears 2+ times in text)
    #   - trusted alias / resolved coreference (future)
    #   - heading/list continuation (future)
    #   - predicate-specific discourse rule (future)
    _scope_validating_syntax = any(
        se.canonical_predicate and se.canonical_predicate.strip()
        for se in evidence.syntax_evidence
    )
    if not evidence.same_sentence and not _scope_validating_syntax:
        # P4-lite hardening: check typed link against per-predicate allowlist.
        link_type = evidence.cross_sentence_link_type or "none"
        allowed_links = _allowed_links_for_predicate(top.predicate)
        link_authorized = link_type in allowed_links

        if evidence.sentence_distance <= 1 and evidence.has_cross_sentence_link and link_authorized:
            # Adjacent with explicit, predicate-approved link → hold for review.
            return GateDecision(
                status=GateStatus.REVIEW_CROSS_SENTENCE,
                predicate=top.predicate,
                score=top.score,
                margin=margin,
                direction_margin=direction_margin,
                logit_margin=logit_margin,
                logit_direction_margin=logit_direction_margin,
                reasons=(
                    "cross_sentence_adjacent_with_link",
                    f"distance={evidence.sentence_distance}",
                    f"link_type={link_type}",
                ),
                blocking_reasons=("CROSS_SENTENCE_ADJACENT",),
            )
        # Adjacent without link, with disallowed link type, OR distant → reject.
        if evidence.sentence_distance <= 1 and evidence.has_cross_sentence_link and not link_authorized:
            scope_reason = f"cross_sentence_link_type_not_allowed:{link_type}"
        elif evidence.sentence_distance <= 1:
            scope_reason = "cross_sentence_no_link"
        else:
            scope_reason = f"cross_sentence_distant_{evidence.sentence_distance}"
        return GateDecision(
            status=GateStatus.REJECT_SCOPE,
            predicate=top.predicate,
            score=top.score,
            margin=margin,
            direction_margin=direction_margin,
            logit_margin=logit_margin,
            logit_direction_margin=logit_direction_margin,
            reasons=(scope_reason,),
            blocking_reasons=(
                "SCOPE_REJECTED",
                f"SENTENCE_DISTANCE_{evidence.sentence_distance}",
            ),
        )

    matching_syntax = [
        item
        for item in evidence.syntax_evidence
        if item.canonical_predicate == top.predicate
        and not item.negated
        and JoinMode(item.join_mode) not in SHADOW_ONLY_JOIN_MODES
    ]

    # Partition conflicting evidence by join trust.
    #
    # A syntax record only counts as a STRONG conflict when its join_mode
    # has trust >= CONFLICT_TRUST_FLOOR (i.e. NOT a shadow-only join).
    # `normalized_surface` joins (trust 0.30) are diagnostic and cannot
    # independently trigger REVIEW_CONFLICT — they exist for visibility.
    #
    # An EMPTY canonical_predicate means the resolver could not map the surface
    # verb to any ontology predicate. That is NOT a disagreement — it is an
    # absence of a canonical claim. Only non-empty predicates that differ from
    # Relex's top-1 count as conflicts.
    conflicting_syntax_trusted: list = []
    conflicting_syntax_shadow_only: list = []
    for item in evidence.syntax_evidence:
        if item.negated:
            continue
        if not item.canonical_predicate or not item.canonical_predicate.strip():
            continue  # unmapped surface — not a conflict, handled by STORE_UNMAPPED
        if item.canonical_predicate == top.predicate:
            continue
        try:
            mode = JoinMode(item.join_mode)
        except ValueError:
            mode = JoinMode.EXACT_SPAN
        if mode in SHADOW_ONLY_JOIN_MODES:
            conflicting_syntax_shadow_only.append(item)
        elif JOIN_TRUST.get(mode, 1.0) >= CONFLICT_TRUST_FLOOR:
            conflicting_syntax_trusted.append(item)
        else:
            conflicting_syntax_shadow_only.append(item)

    # --- REVIEW_CONFLICT: strong trusted syntax disagrees with Relex top-1 ---
    #
    # This is evaluated BEFORE the standard acceptance gate so a high Relex
    # score cannot bypass deterministic disagreement. If trusted syntax
    # (exact_span / canonical_entity / resolved_alias) asserts a different
    # predicate, the pair is routed to REVIEW regardless of score.
    #
    # Shadow-only disagreements (normalized_surface joins) do NOT trigger
    # REVIEW_CONFLICT — they are attached to the decision as informational
    # signals so reviewers can see the disagreement without it blocking.
    if conflicting_syntax_trusted:
        conflict_preds = tuple(
            sorted(
                {item.canonical_predicate for item in conflicting_syntax_trusted}
            )
        )
        shadow_preds = tuple(
            sorted({item.canonical_predicate
                    for item in conflicting_syntax_shadow_only})
        ) if conflicting_syntax_shadow_only else ()
        return GateDecision(
            status=GateStatus.REVIEW_CONFLICT,
            predicate=top.predicate,
            score=top.score,
            margin=margin,
            direction_margin=direction_margin,
            logit_margin=logit_margin,
            logit_direction_margin=logit_direction_margin,
            reasons=(
                "syntax_relex_disagreement",
                *conflict_preds,
                *shadow_preds,
            ),
            blocking_reasons=("SYNTAX_RELEX_DISAGREEMENT",),
        )

    # --- Standard high-confidence acceptance ---
    if (
        top.score >= predicate_policy.standard_threshold
        and margin >= predicate_policy.standard_margin
        and _direction_is_valid(direction_margin, predicate_policy, evidence)
    ):
        if matching_syntax:
            # Syntax agrees with Relex top-1 → full production acceptance.
            status = _acceptance_status(
                GateStatus.ACCEPT_HIGH, evidence, predicate_policy,
            )
            return GateDecision(
                status=status,
                predicate=top.predicate,
                score=top.score,
                margin=margin,
                direction_margin=direction_margin,
                logit_margin=logit_margin,
                logit_direction_margin=logit_direction_margin,
                reasons=("relex_standard_gate_passed", "syntax_agrees"),
            )
        # Relex-only: no syntax support. Shadow until precision is verified.
        # Production eligibility requires ACCEPT_CORROBORATED or
        # ACCEPT_SYNTAX_HIGH; a lane with unknown precision must not write
        # knowledge.  Promote back when per-lane precision ≥ 0.90.
        status = _acceptance_status(
            GateStatus.SHADOW_RELEX_HIGH, evidence, predicate_policy,
        )
        return GateDecision(
            status=status,
            predicate=top.predicate,
            score=top.score,
            margin=margin,
            direction_margin=direction_margin,
            logit_margin=logit_margin,
            logit_direction_margin=logit_direction_margin,
            reasons=("relex_standard_gate_passed", "relex_only_shadow"),
            blocking_reasons=("NO_SYNTAX_SUPPORT",),
        )

    # --- Corroborated acceptance: lowered threshold + syntax agreement ---
    if (
        matching_syntax
        and top.score >= predicate_policy.corroborated_threshold
        and margin >= predicate_policy.corroborated_margin
        and _direction_is_valid(direction_margin, predicate_policy, evidence)
    ):
        status = _acceptance_status(
            GateStatus.ACCEPT_CORROBORATED, evidence, predicate_policy,
        )
        return GateDecision(
            status=status,
            predicate=top.predicate,
            score=top.score,
            margin=margin,
            direction_margin=direction_margin,
            logit_margin=logit_margin,
            logit_direction_margin=logit_direction_margin,
            reasons=(
                "relex_top1",
                "syntax_corroborated",
                matching_syntax[0].pattern_id,
            ),
        )

    # --- Conflict: syntax disagrees with Relex top-1 ---
    # (Strong trusted conflicts were already handled above. Shadow-only
    # disagreements from normalized_surface joins do NOT trigger
    # REVIEW_CONFLICT — they are too low-trust to override the model. The
    # pair falls through to REJECT_LOW_EVIDENCE or is accepted by the
    # corroborated gate if matching syntax agrees.)

    # --- Unmapped surface relation fallthrough ---
    #
    # Before rejecting, check whether trusted syntax licensed a relation whose
    # surface predicate the resolver could not map to any canonical predicate.
    # Route based on direction confidence:
    #   high + agent subject → STORE_UNMAPPED_SURFACE_RELATION (trusted)
    #   medium / patient subject → REVIEW_UNMAPPED_RELATION (ambiguous direction)
    trusted_unmapped = [
        s for s in evidence.syntax_evidence
        if JoinMode(s.join_mode) not in SHADOW_ONLY_JOIN_MODES
        and (not s.canonical_predicate or not s.canonical_predicate.strip())
        and s.surface_predicate and s.surface_predicate.strip()
        and not s.negated
    ]
    if trusted_unmapped:
        best = trusted_unmapped[0]
        direction_trusted = _direction_is_trusted(best)
        if direction_trusted:
            status = GateStatus.STORE_UNMAPPED_SURFACE_RELATION
            reason_tag = "no_semantically_adequate_canonical_mapping"
        else:
            status = GateStatus.REVIEW_UNMAPPED_RELATION
            reason_tag = "direction_ambiguous"
        return GateDecision(
            status=status,
            predicate=top.predicate,
            score=top.score,
            margin=margin,
            direction_margin=direction_margin,
            logit_margin=logit_margin,
            logit_direction_margin=logit_direction_margin,
            reasons=(
                reason_tag,
                best.surface_predicate,
                best.pattern_id,
                f"dir_conf={best.direction_confidence}",
                f"subj_role={best.subject_dependency_role}",
            ),
        )

    return GateDecision(
        status=GateStatus.REJECT_LOW_EVIDENCE,
        predicate=top.predicate,
        score=top.score,
        margin=margin,
        direction_margin=direction_margin,
        logit_margin=logit_margin,
        logit_direction_margin=logit_direction_margin,
        reasons=("insufficient_combined_evidence",),
        blocking_reasons=("SCORE_BELOW_THRESHOLD",),
    )


# ---------------------------------------------------------------------------
# Syntax-only evaluation (no Relex predicate scores)
# ---------------------------------------------------------------------------


def _evaluate_syntax_only(
    evidence: RelationEvidence,
    negated_items: list,
    policy: RelationPolicy,
) -> GateDecision:
    """Evaluate pairs that have syntax evidence but no Relex predicate scores.

    This is the syntax → Relex entry direction: a FrameExtractor triple for a
    pair that Relex did not preserve in its decoded output. Without this path,
    such pairs silently disappear from the combined process.

    Decision order:
      1. Negated syntax → REJECT_NEGATED
      2. No trusted syntax → REJECT_LOW_EVIDENCE
      3. Trusted syntax with known canonical predicate:
         a. Valid endpoint types + valid direction → ACCEPT_SYNTAX_HIGH
         b. Otherwise → REVIEW_SYNTAX_ONLY
      4. Trusted syntax with meaningful surface, no canonical mapping
         → STORE_UNMAPPED_SURFACE_RELATION
      5. Otherwise → REJECT_LOW_EVIDENCE

    ACCEPT_SYNTAX_HIGH requires ALL of:
      - Trusted syntax (non-shadow join mode)
      - Unambiguous canonical predicate mapping
      - Valid endpoint types per predicate policy
      - Positive assertion (not negated — checked upstream)
      - No self-loop (checked upstream in evaluate_relation)
    """
    # --- Negation gate ---
    if negated_items:
        return GateDecision(
            status=GateStatus.REJECT_NEGATED,
            predicate=None,
            score=0.0,
            margin=0.0,
            direction_margin=0.0,
            reasons=("negated_relation_evidence", negated_items[0].pattern_id),
        )

    # Partition syntax by trust. Shadow-only joins (normalized_surface,
    # span_containment) cannot independently authorize a production write.
    trusted = [
        s for s in evidence.syntax_evidence
        if JoinMode(s.join_mode) not in SHADOW_ONLY_JOIN_MODES
    ]

    if not trusted:
        return GateDecision(
            status=GateStatus.REJECT_LOW_EVIDENCE,
            predicate=None,
            score=0.0,
            margin=0.0,
            direction_margin=0.0,
            reasons=("no_trusted_syntax",),
        )

    # Case 1: trusted syntax carries a known canonical predicate.
    resolved = [
        s for s in trusted
        if s.canonical_predicate and s.canonical_predicate.strip()
    ]
    if resolved:
        canonical_pred = resolved[0].canonical_predicate
        pred_policy = policy[canonical_pred]

        # Endpoint type validation
        type_ok = pred_policy.type_compatibility(
            evidence.subject_type, evidence.object_type,
        ) != "invalid"

        # Direction validation: syntax-only uses the dependency frame direction.
        # Without Relex reverse scores, we trust the frame if the pattern
        # indicates agent→patient order (active transitive, prep object, copula).
        # Pattern IDs may be prefixed ("active_transitive:nsubj-VERB-dobj") or
        # bare ("nsubj-VERB-dobj").
        pid = resolved[0].pattern_id
        direction_ok = (
            pid.startswith(("active_transitive", "prep_object", "copula"))
            or "nsubj-VERB-dobj" in pid
            or "nsubj-VERB-prep" in pid
            or "nsubj-VERB-xcomp" in pid
        )

        if type_ok and direction_ok:
            return GateDecision(
                status=GateStatus.ACCEPT_SYNTAX_HIGH,
                predicate=canonical_pred,
                score=0.0,
                margin=0.0,
                direction_margin=0.0,
                reasons=(
                    "syntax_only_deterministic",
                    "no_relex_evidence",
                    resolved[0].pattern_id,
                ),
            )
        # Insufficient for acceptance — route to review.
        blockers = []
        if not type_ok:
            blockers.append("endpoint_type_invalid")
        if not direction_ok:
            blockers.append("direction_unverified")
        return GateDecision(
            status=GateStatus.REVIEW_SYNTAX_ONLY,
            predicate=canonical_pred,
            score=0.0,
            margin=0.0,
            direction_margin=0.0,
            reasons=(
                "syntax_only_insufficient_for_acceptance",
                resolved[0].pattern_id,
                *blockers,
            ),
            blocking_reasons=tuple(b.upper() for b in blockers),
        )

    # Case 2: trusted syntax licensed a relation with a meaningful surface
    # predicate, but the resolver could not map it to any canonical predicate.
    # Route based on construction-level direction trust:
    #   high confidence + complete slots + approved role ordering → STORE
    #   otherwise → REVIEW
    meaningful = [
        s for s in trusted
        if s.surface_predicate and s.surface_predicate.strip()
    ]
    if meaningful:
        best = meaningful[0]
        direction_trusted = _direction_is_trusted(best)
        if direction_trusted:
            status = GateStatus.STORE_UNMAPPED_SURFACE_RELATION
            reason_tag = "no_semantically_adequate_canonical_mapping"
        else:
            status = GateStatus.REVIEW_UNMAPPED_RELATION
            reason_tag = "direction_ambiguous"
        return GateDecision(
            status=status,
            predicate=None,
            score=0.0,
            margin=0.0,
            direction_margin=0.0,
            reasons=(
                reason_tag,
                best.surface_predicate,
                best.pattern_id,
                f"dir_conf={best.direction_confidence}",
                f"subj_role={best.subject_dependency_role}",
            ),
        )

    return GateDecision(
        status=GateStatus.REJECT_LOW_EVIDENCE,
        predicate=None,
        score=0.0,
        margin=0.0,
        direction_margin=0.0,
        reasons=("no_meaningful_surface_predicate",),
    )

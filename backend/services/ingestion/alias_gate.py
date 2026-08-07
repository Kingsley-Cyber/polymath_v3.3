"""Deterministic alias gate (Alias Pipeline Phase 4).

Maps AliasCandidateV1 → AliasDecisionV1 under the owner-approved decision
policy. Never mutates production corpora or activates Fast schema expansion.

Incomplete candidates are fail-closed: they never receive ACCEPT_* decisions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from models.alias_identity import (
    ALIAS_GATE_RELEASE,
    AliasCandidateV1,
    AliasDecisionKind,
    AliasDecisionV1,
    AliasScope,
    AmbiguityStatus,
    IncompleteAliasCandidate,
)

GATE_RELEASE = ALIAS_GATE_RELEASE

# Deterministic reason codes (stable strings; hashed into decision_hash).
REASON_ACCEPT_EXPLICIT_ALTERNATE = "GATE_ACCEPT_EXPLICIT_ALTERNATE_NAME_V1"
REASON_ACCEPT_EXPLICIT_PATTERN = "GATE_ACCEPT_EXPLICIT_ALIAS_PATTERN_V1"
REASON_ACCEPT_ACRONYM = "GATE_ACCEPT_ACRONYM_LONG_FORM_V1"
REASON_ACCEPT_ABBREV = "GATE_ACCEPT_EXPLICIT_ABBREVIATION_V1"
REASON_ACCEPT_TEMPORAL = "GATE_ACCEPT_TEMPORAL_FORMER_NAME_V1"
REASON_ACCEPT_CURATED = "GATE_ACCEPT_CURATED_ALIAS_V1"
REASON_ACCEPT_CASING = "GATE_ACCEPT_CASING_VARIANT_V1"
REASON_ACCEPT_PUNCT = "GATE_ACCEPT_PUNCTUATION_VARIANT_V1"
REASON_ACCEPT_APPOS_EXPLICIT = "GATE_ACCEPT_PROPER_APPOS_EXPLICIT_SIGNAL_V1"
REASON_ACCEPT_SYNONYM_CORROBORATED = "GATE_ACCEPT_RELEX_SYNONYM_CORROBORATED_V1"
REASON_RETRIEVAL_SURFACE = "GATE_RETRIEVAL_EXTRACTION_SURFACE_VARIANT_V1"
REASON_RETRIEVAL_MORPH = "GATE_RETRIEVAL_MORPHOLOGICAL_VARIANT_V1"
REASON_RETRIEVAL_SYNONYM = "GATE_RETRIEVAL_RELEX_SYNONYM_UNCORROBORATED_V1"
REASON_REVIEW_PROPER_APPOS = "GATE_REVIEW_PROPER_NAME_APPOSITION_V1"
REASON_REVIEW_SEMANTIC_SHADOW = "GATE_REVIEW_SEMANTIC_RELATED_SHADOW_ONLY_V1"
REASON_REVIEW_AMBIGUOUS_ACRONYM = "GATE_REVIEW_AMBIGUOUS_ACRONYM_DOCUMENT_V1"
REASON_REVIEW_TYPE_CONFLICT = "GATE_REVIEW_ENTITY_TYPE_CONFLICT_V1"
REASON_REVIEW_ACRONYM_ALIGN = "GATE_REVIEW_ACRONYM_ALIGNMENT_FAILED_V1"
REASON_REJECT_DESCRIPTIVE = "GATE_REJECT_DESCRIPTIVE_APPOSITION_V1"
REASON_REJECT_ROLE = "GATE_REJECT_ROLE_APPOSITION_V1"
REASON_REJECT_LOCATION = "GATE_REJECT_LOCATION_APPOSITION_V1"
REASON_REJECT_MISSING_EVIDENCE = "GATE_REJECT_MISSING_EVIDENCE_V1"
REASON_REJECT_MISSING_RULE = "GATE_REJECT_MISSING_RULE_ID_V1"
REASON_REJECT_INVALID_SPANS = "GATE_REJECT_INVALID_SOURCE_SPANS_V1"
REASON_REJECT_MISSING_SCOPE = "GATE_REJECT_MISSING_SCOPE_V1"
REASON_REJECT_CASING_MISMATCH = "GATE_REJECT_CASING_VARIANT_MISMATCH_V1"
REASON_REJECT_UNKNOWN_TYPE = "GATE_REJECT_UNKNOWN_CANDIDATE_TYPE_V1"
REASON_REJECT_INCOMPLETE = "GATE_REJECT_INCOMPLETE_PROVENANCE_V1"

_STOPWORDS = frozenset(
    {"a", "an", "the", "of", "and", "or", "for", "to", "in", "on", "by", "with"}
)

_TYPE_CANON = {
    "person": "person",
    "per": "person",
    "people": "person",
    "org": "organization",
    "organization": "organization",
    "organisation": "organization",
    "company": "organization",
    "corp": "organization",
    "gpe": "location",
    "loc": "location",
    "location": "location",
    "place": "location",
    "concept": "concept",
    "product": "product",
    "work_of_art": "work_of_art",
    "event": "event",
}

_EXPLICIT_IDENTITY_TYPES = frozenset(
    {
        "explicit_alternate_name",
        "explicit_alias_pattern",
        "acronym_long_form",
        "explicit_abbreviation",
        "former_name",
        "curated_alias",
        "casing_variant",
        "punctuation_variant",
    }
)

_KNOWN_AS_RULE_PREFIXES = ("APPOS_KNOWN_AS",)


@dataclass(frozen=True)
class AliasGateBatch:
    """Gate outputs. Incomplete rows never become ACCEPT_*."""

    decisions: list[AliasDecisionV1] = field(default_factory=list)
    incomplete: list[IncompleteAliasCandidate] = field(default_factory=list)
    gate_release: str = GATE_RELEASE


def _norm_surface(value: str) -> str:
    return " ".join((value or "").lower().split())


def _norm_tokens_alnum(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (value or "").lower())


def _norm_type(value: str | None) -> str | None:
    if value is None:
        return None
    key = value.strip().lower()
    if not key:
        return None
    return _TYPE_CANON.get(key, key)


def _types_compatible(a: str | None, b: str | None) -> bool:
    na, nb = _norm_type(a), _norm_type(b)
    if na is None or nb is None:
        return True
    return na == nb


def _acronym_character_alignment(short: str, long_form: str) -> bool:
    short_letters = re.sub(r"[^A-Za-z0-9]", "", short or "").upper()
    if len(short_letters) < 2:
        return False
    tokens = _norm_tokens_alnum(long_form)
    if not tokens:
        return False
    significant = [t for t in tokens if t not in _STOPWORDS] or tokens
    initials = "".join(t[0].upper() for t in significant if t)
    all_initials = "".join(t[0].upper() for t in tokens if t)
    if short_letters == initials or short_letters == all_initials:
        return True
    # Allow short form drawn from significant initials as subsequence prefix.
    if initials.startswith(short_letters) or short_letters.startswith(initials[: len(short_letters)]):
        return len(short_letters) >= 2 and short_letters[0] == initials[0]
    return False


def _identical_normalized_token_sequence(a: str, b: str) -> bool:
    return _norm_tokens_alnum(a) == _norm_tokens_alnum(b) and bool(_norm_tokens_alnum(a))


def _identical_after_punct_norm(a: str, b: str) -> bool:
    def _p(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    return bool(_p(a)) and _p(a) == _p(b)


def _has_explicit_appos_signal(candidate: AliasCandidateV1) -> bool:
    return any(candidate.rule_id.startswith(p) for p in _KNOWN_AS_RULE_PREFIXES)


def _pair_key(candidate: AliasCandidateV1) -> tuple[str, str]:
    a = _norm_surface(candidate.canonical_surface)
    b = _norm_surface(candidate.candidate_surface)
    return (a, b) if a <= b else (b, a)


def _provenance_reject_reason(candidate: AliasCandidateV1) -> str | None:
    """Fail-closed provenance checks beyond pydantic construction."""

    if not (candidate.evidence_text or "").strip():
        return REASON_REJECT_MISSING_EVIDENCE
    if not (candidate.rule_id or "").strip():
        return REASON_REJECT_MISSING_RULE
    if not candidate.scope:
        return REASON_REJECT_MISSING_SCOPE
    if (
        candidate.canonical_end <= candidate.canonical_start
        or candidate.candidate_end <= candidate.candidate_start
        or candidate.canonical_start < 0
        or candidate.candidate_start < 0
    ):
        return REASON_REJECT_INVALID_SPANS
    return None


def _decision(
    *,
    candidate: AliasCandidateV1,
    decision: AliasDecisionKind,
    decision_reason: str,
    identity_merge_allowed: bool,
    retrieval_expansion_allowed: bool,
    scope: AliasScope,
    ambiguity_status: AmbiguityStatus,
) -> AliasDecisionV1:
    return AliasDecisionV1.create(
        alias_candidate_id=candidate.alias_candidate_id,
        decision=decision,
        decision_reason=decision_reason,
        identity_merge_allowed=identity_merge_allowed,
        retrieval_expansion_allowed=retrieval_expansion_allowed,
        scope=scope,
        ambiguity_status=ambiguity_status,
        gate_release=GATE_RELEASE,
    )


def _accept_identity(
    candidate: AliasCandidateV1,
    reason: str,
    *,
    scope: AliasScope | None = None,
    ambiguity_status: AmbiguityStatus = "unambiguous",
) -> AliasDecisionV1:
    return _decision(
        candidate=candidate,
        decision="ACCEPT_IDENTITY",
        decision_reason=reason,
        identity_merge_allowed=True,
        retrieval_expansion_allowed=True,
        scope=scope or candidate.scope,
        ambiguity_status=ambiguity_status,
    )


def _accept_temporal(candidate: AliasCandidateV1, reason: str) -> AliasDecisionV1:
    return _decision(
        candidate=candidate,
        decision="ACCEPT_TEMPORAL_IDENTITY",
        decision_reason=reason,
        identity_merge_allowed=True,
        retrieval_expansion_allowed=True,
        scope=candidate.scope,
        ambiguity_status="unambiguous",
    )


def _retrieval_only(
    candidate: AliasCandidateV1,
    reason: str,
    *,
    ambiguity_status: AmbiguityStatus = "not_applicable",
) -> AliasDecisionV1:
    return _decision(
        candidate=candidate,
        decision="ACCEPT_RETRIEVAL_ONLY",
        decision_reason=reason,
        identity_merge_allowed=False,
        retrieval_expansion_allowed=True,
        scope=candidate.scope,
        ambiguity_status=ambiguity_status,
    )


def _review(
    candidate: AliasCandidateV1,
    reason: str,
    *,
    ambiguity_status: AmbiguityStatus = "insufficient_evidence",
    retrieval_expansion_allowed: bool = False,
    scope: AliasScope | None = None,
) -> AliasDecisionV1:
    return _decision(
        candidate=candidate,
        decision="REVIEW",
        decision_reason=reason,
        identity_merge_allowed=False,
        retrieval_expansion_allowed=retrieval_expansion_allowed,
        scope=scope or candidate.scope,
        ambiguity_status=ambiguity_status,
    )


def _reject(
    candidate: AliasCandidateV1,
    reason: str,
    *,
    ambiguity_status: AmbiguityStatus = "not_applicable",
) -> AliasDecisionV1:
    return _decision(
        candidate=candidate,
        decision="REJECT",
        decision_reason=reason,
        identity_merge_allowed=False,
        retrieval_expansion_allowed=False,
        scope=candidate.scope,
        ambiguity_status=ambiguity_status,
    )


def _build_doc_acronym_conflicts(
    candidates: Sequence[AliasCandidateV1],
) -> set[tuple[str, str]]:
    """Return {(document_id, short_upper)} where short maps to >1 long form."""

    mapping: dict[tuple[str, str], set[str]] = {}
    for cand in candidates:
        if cand.candidate_type not in {"acronym_long_form", "explicit_abbreviation"}:
            continue
        short = cand.candidate_surface
        long_form = cand.canonical_surface
        # Prefer shorter side as acronym.
        if len(_norm_surface(cand.canonical_surface)) < len(_norm_surface(cand.candidate_surface)):
            short, long_form = cand.canonical_surface, cand.candidate_surface
        short_key = re.sub(r"[^A-Za-z0-9]", "", short).upper()
        if len(short_key) < 2:
            continue
        key = (cand.document_id, short_key)
        mapping.setdefault(key, set()).add(_norm_surface(long_form))
    return {k for k, longs in mapping.items() if len(longs) > 1}


def _build_corroboration_pairs(
    candidates: Sequence[AliasCandidateV1],
) -> set[tuple[str, tuple[str, str]]]:
    """Pairs from deterministic explicit/acronym/curated candidates (pre-ambiguity)."""

    pairs: set[tuple[str, tuple[str, str]]] = set()
    for cand in candidates:
        if cand.candidate_type not in _EXPLICIT_IDENTITY_TYPES:
            continue
        if _provenance_reject_reason(cand):
            continue
        if not _types_compatible(cand.entity_type, cand.candidate_entity_type):
            continue
        if cand.candidate_type == "acronym_long_form":
            short, long_form = cand.candidate_surface, cand.canonical_surface
            if len(_norm_surface(cand.canonical_surface)) < len(
                _norm_surface(cand.candidate_surface)
            ):
                short, long_form = cand.canonical_surface, cand.candidate_surface
            if not _acronym_character_alignment(short, long_form):
                continue
        if cand.candidate_type == "casing_variant" and not _identical_normalized_token_sequence(
            cand.canonical_surface, cand.candidate_surface
        ):
            continue
        if cand.candidate_type == "punctuation_variant" and not _identical_after_punct_norm(
            cand.canonical_surface, cand.candidate_surface
        ):
            continue
        pairs.add((cand.document_id, _pair_key(cand)))
    return pairs


def _decide_one(
    candidate: AliasCandidateV1,
    *,
    acronym_conflicts: set[tuple[str, str]],
    corroboration_pairs: set[tuple[str, tuple[str, str]]],
) -> AliasDecisionV1:
    prov = _provenance_reject_reason(candidate)
    if prov:
        return _reject(candidate, prov, ambiguity_status="insufficient_evidence")

    if not _types_compatible(candidate.entity_type, candidate.candidate_entity_type):
        return _review(
            candidate,
            REASON_REVIEW_TYPE_CONFLICT,
            ambiguity_status="type_conflict",
        )

    ctype = candidate.candidate_type

    if ctype == "descriptive_apposition":
        return _reject(candidate, REASON_REJECT_DESCRIPTIVE)
    if ctype == "role_apposition":
        return _reject(candidate, REASON_REJECT_ROLE)
    if ctype == "location_apposition":
        return _reject(candidate, REASON_REJECT_LOCATION)

    if ctype == "semantic_related_term":
        # Shadow-only: no identity, no retrieval expansion.
        return _review(
            candidate,
            REASON_REVIEW_SEMANTIC_SHADOW,
            ambiguity_status="not_applicable",
            retrieval_expansion_allowed=False,
        )

    if ctype == "extraction_surface_variant":
        return _retrieval_only(candidate, REASON_RETRIEVAL_SURFACE)

    if ctype == "morphological_variant":
        return _retrieval_only(candidate, REASON_RETRIEVAL_MORPH)

    if ctype == "relex_synonym_relation":
        if (candidate.document_id, _pair_key(candidate)) in corroboration_pairs:
            return _accept_identity(candidate, REASON_ACCEPT_SYNONYM_CORROBORATED)
        return _retrieval_only(candidate, REASON_RETRIEVAL_SYNONYM)

    if ctype == "proper_name_apposition":
        if _has_explicit_appos_signal(candidate):
            return _accept_identity(candidate, REASON_ACCEPT_APPOS_EXPLICIT)
        return _review(
            candidate,
            REASON_REVIEW_PROPER_APPOS,
            ambiguity_status="insufficient_evidence",
        )

    if ctype == "former_name":
        return _accept_temporal(candidate, REASON_ACCEPT_TEMPORAL)

    if ctype == "explicit_alternate_name":
        return _accept_identity(candidate, REASON_ACCEPT_EXPLICIT_ALTERNATE)

    if ctype == "explicit_alias_pattern":
        return _accept_identity(candidate, REASON_ACCEPT_EXPLICIT_PATTERN)

    if ctype == "curated_alias":
        return _accept_identity(candidate, REASON_ACCEPT_CURATED, scope="corpus")

    if ctype == "casing_variant":
        if _identical_normalized_token_sequence(
            candidate.canonical_surface, candidate.candidate_surface
        ):
            return _accept_identity(candidate, REASON_ACCEPT_CASING)
        return _reject(candidate, REASON_REJECT_CASING_MISMATCH)

    if ctype == "punctuation_variant":
        if _identical_after_punct_norm(
            candidate.canonical_surface, candidate.candidate_surface
        ):
            return _accept_identity(candidate, REASON_ACCEPT_PUNCT)
        return _reject(candidate, REASON_REJECT_CASING_MISMATCH)

    if ctype in {"acronym_long_form", "explicit_abbreviation"}:
        short = candidate.candidate_surface
        long_form = candidate.canonical_surface
        if len(_norm_surface(candidate.canonical_surface)) < len(
            _norm_surface(candidate.candidate_surface)
        ):
            short, long_form = candidate.canonical_surface, candidate.candidate_surface
        short_key = re.sub(r"[^A-Za-z0-9]", "", short).upper()
        if (candidate.document_id, short_key) in acronym_conflicts:
            return _review(
                candidate,
                REASON_REVIEW_AMBIGUOUS_ACRONYM,
                ambiguity_status="ambiguous_acronym",
                scope="document",
                retrieval_expansion_allowed=False,
            )
        if ctype == "acronym_long_form" and not _acronym_character_alignment(
            short, long_form
        ):
            return _review(
                candidate,
                REASON_REVIEW_ACRONYM_ALIGN,
                ambiguity_status="insufficient_evidence",
                scope="document",
            )
        reason = (
            REASON_ACCEPT_ACRONYM
            if ctype == "acronym_long_form"
            else REASON_ACCEPT_ABBREV
        )
        return _accept_identity(
            candidate,
            reason,
            scope="document",
            ambiguity_status="unambiguous",
        )

    return _reject(candidate, REASON_REJECT_UNKNOWN_TYPE)


def run_alias_gate(
    candidates: Iterable[AliasCandidateV1] | None,
    *,
    incomplete: Iterable[IncompleteAliasCandidate] | None = None,
) -> AliasGateBatch:
    """Apply the versioned decision policy. Pure / deterministic / side-effect free."""

    rows = list(candidates or [])
    incomplete_rows = list(incomplete or [])

    # Sort for deterministic replay independent of caller order.
    rows.sort(
        key=lambda c: (
            c.document_id,
            c.chunk_id,
            c.candidate_type,
            c.rule_id,
            c.canonical_start,
            c.candidate_start,
            c.alias_candidate_id,
        )
    )

    acronym_conflicts = _build_doc_acronym_conflicts(rows)
    corroboration_pairs = _build_corroboration_pairs(rows)

    decisions = [
        _decide_one(
            cand,
            acronym_conflicts=acronym_conflicts,
            corroboration_pairs=corroboration_pairs,
        )
        for cand in rows
    ]

    # Stable incomplete order; never promoted.
    incomplete_rows.sort(
        key=lambda i: (
            i.document_id,
            i.chunk_id,
            i.source_method,
            i.canonical_surface.lower(),
            i.candidate_surface.lower(),
            i.incomplete_reason,
        )
    )

    return AliasGateBatch(
        decisions=decisions,
        incomplete=incomplete_rows,
        gate_release=GATE_RELEASE,
    )


def incomplete_never_accepted(batch: AliasGateBatch) -> bool:
    """Invariant helper for tests / callers."""

    accepted_ids = {
        d.alias_candidate_id
        for d in batch.decisions
        if d.decision
        in {"ACCEPT_IDENTITY", "ACCEPT_TEMPORAL_IDENTITY", "ACCEPT_RETRIEVAL_ONLY"}
    }
    # Incomplete rows have no alias_candidate_id field that could match.
    return all(
        getattr(row, "alias_candidate_id", None) not in accepted_ids
        for row in batch.incomplete
    )

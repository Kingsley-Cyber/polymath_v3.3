"""Alias candidate unification around live miners (Phases 2–3).

Wraps extract_aliases / Schwartz-Hearst, corpus_lexicon explicit patterns,
curated entity_aliases.json, Relex surface variants, and typed appositions
into AliasCandidateV1 when provenance is complete. Incomplete observations
are returned separately.

Does NOT:
  - mutate production corpora
  - run the alias gate
  - activate Fast schema expansion
  - replace legacy query_aliases (always preserved via extract_aliases)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from models.alias_identity import (
    ALIAS_PIPELINE_RELEASE,
    AliasCandidateType,
    AliasCandidateV1,
    AliasScope,
    IncompleteAliasCandidate,
)
from services.extraction.canonical import (
    canonicalize_entity_name,
    normalize_entity_name,
    resolve_entity_alias,
)
from services.ingestion.enrich import (
    _casing_variants,
    extract_aliases,
    norm,
    schwartz_hearst_matches,
)

RULE_RELEASE = "alias_candidate_rules.v1"
EXTRACTOR_RELEASE = ALIAS_PIPELINE_RELEASE

_EXPLICIT_ALIAS_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<left>[A-Za-z0-9][A-Za-z0-9'&/\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9'&/\-]*){0,9})"
    r"(?![A-Za-z0-9])\s*"
    r"(?:,?\s*(?:also\s+known\s+as|also\s+called|a\.?k\.?a\.?|short\s+for))\s+"
    r"(?P<right>[A-Za-z0-9][A-Za-z0-9'&/\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9'&/\-]*){0,9})"
    r"(?=[,.;:!?\n]|$)",
    re.IGNORECASE,
)

_ABBREVIATED_AS_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<long>[A-Za-z0-9][A-Za-z0-9'&/\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9'&/\-]*){0,9})"
    r"(?![A-Za-z0-9])\s*,?\s*abbreviated(?:\s+as)?\s+"
    r"(?P<short>[A-Z][A-Z0-9\-]{1,9})\b",
    re.IGNORECASE,
)

_FORMERLY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<current>[A-Za-z0-9][A-Za-z0-9'&/\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9'&/\-]*){0,9})"
    r"(?![A-Za-z0-9])\s*,?\s*formerly\s+known\s+as\s+"
    r"(?P<former>[A-Za-z0-9][A-Za-z0-9'&/\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9'&/\-]*){0,9})"
    r"(?=[,.;:!?\n]|$)",
    re.IGNORECASE,
)

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class AliasCandidateBatch:
    """Typed candidates + incomplete observations + legacy string aliases."""

    candidates: list[AliasCandidateV1] = field(default_factory=list)
    incomplete: list[IncompleteAliasCandidate] = field(default_factory=list)
    legacy_query_aliases: dict[str, list[str]] = field(default_factory=dict)


def _entity_surfaces(entity: dict[str, Any]) -> set[str]:
    canon = norm(entity.get("canonical_name") or entity.get("surface_form") or "")
    surface = norm(entity.get("surface_form") or canon)
    return {s.lower() for s in (canon, surface) if s}


def _sentence_id_for_offset(text: str, chunk_id: str, offset: int) -> str:
    """Deterministic sentence id from chunk + sentence index containing offset."""

    idx = 0
    cursor = 0
    # Prefer newline-aware units similar to enrich._sentences, but keep simple
    # punctuation split for stable ids when headings are absent.
    for line in (text or "").split("\n"):
        line_start = text.find(line, cursor) if line else cursor
        if line_start < 0:
            line_start = cursor
        cursor = line_start + len(line) + 1
        stripped = line.strip()
        if not stripped:
            continue
        local = 0
        for sent in _SENT_SPLIT.split(stripped):
            sent = sent.strip()
            if not sent:
                continue
            abs_start = line_start + line.find(sent, local)
            abs_end = abs_start + len(sent)
            local = abs_end - line_start
            if abs_start <= offset < abs_end or (
                offset == abs_end and abs_start < abs_end
            ):
                return f"{chunk_id}:sent:{idx}"
            idx += 1
    return f"{chunk_id}:sent:0"


def _find_span(text: str, needle: str) -> tuple[int, int] | None:
    if not text or not needle:
        return None
    pos = text.find(needle)
    if pos >= 0:
        return pos, pos + len(needle)
    lower = text.lower()
    n = needle.lower()
    pos = lower.find(n)
    if pos >= 0:
        return pos, pos + len(needle)
    return None


def _incomplete(
    *,
    reason: str,
    source_method: str,
    candidate_type: AliasCandidateType | None = None,
    rule_id: str | None = None,
    canonical_surface: str = "",
    candidate_surface: str = "",
    document_id: str = "",
    chunk_id: str = "",
    evidence_text: str = "",
    scope: AliasScope | None = None,
    confidence: float | None = None,
    canonical_start: int | None = None,
    canonical_end: int | None = None,
    candidate_start: int | None = None,
    candidate_end: int | None = None,
) -> IncompleteAliasCandidate:
    return IncompleteAliasCandidate(
        incomplete_reason=reason,
        candidate_type=candidate_type,
        source_method=source_method,
        rule_id=rule_id,
        rule_release=RULE_RELEASE if rule_id else None,
        canonical_surface=canonical_surface,
        candidate_surface=candidate_surface,
        document_id=document_id,
        chunk_id=chunk_id,
        evidence_text=evidence_text,
        scope=scope,
        confidence=confidence,
        canonical_start=canonical_start,
        canonical_end=canonical_end,
        candidate_start=candidate_start,
        candidate_end=candidate_end,
        extractor_release=EXTRACTOR_RELEASE,
    )


def _emit_or_incomplete(
    *,
    candidate_type: AliasCandidateType,
    source_method: str,
    rule_id: str,
    canonical_surface: str,
    candidate_surface: str,
    document_id: str,
    chunk_id: str,
    text: str,
    canonical_span: tuple[int, int] | None,
    candidate_span: tuple[int, int] | None,
    evidence_text: str,
    scope: AliasScope,
    confidence: float,
    entity_type: str | None = None,
) -> AliasCandidateV1 | IncompleteAliasCandidate:
    if not evidence_text.strip():
        return _incomplete(
            reason="missing_evidence_text",
            source_method=source_method,
            candidate_type=candidate_type,
            rule_id=rule_id,
            canonical_surface=canonical_surface,
            candidate_surface=candidate_surface,
            document_id=document_id,
            chunk_id=chunk_id,
            scope=scope,
            confidence=confidence,
        )
    if canonical_span is None or candidate_span is None:
        return _incomplete(
            reason="missing_source_offsets",
            source_method=source_method,
            candidate_type=candidate_type,
            rule_id=rule_id,
            canonical_surface=canonical_surface,
            candidate_surface=candidate_surface,
            document_id=document_id,
            chunk_id=chunk_id,
            evidence_text=evidence_text,
            scope=scope,
            confidence=confidence,
            canonical_start=None if canonical_span is None else canonical_span[0],
            canonical_end=None if canonical_span is None else canonical_span[1],
            candidate_start=None if candidate_span is None else candidate_span[0],
            candidate_end=None if candidate_span is None else candidate_span[1],
        )
    c0, c1 = canonical_span
    a0, a1 = candidate_span
    if c1 <= c0 or a1 <= a0:
        return _incomplete(
            reason="invalid_source_offsets",
            source_method=source_method,
            candidate_type=candidate_type,
            rule_id=rule_id,
            canonical_surface=canonical_surface,
            candidate_surface=candidate_surface,
            document_id=document_id,
            chunk_id=chunk_id,
            evidence_text=evidence_text,
            scope=scope,
            confidence=confidence,
        )
    sentence_id = _sentence_id_for_offset(text, chunk_id, min(c0, a0))
    return AliasCandidateV1.create(
        candidate_type=candidate_type,
        source_method=source_method,
        rule_id=rule_id,
        rule_release=RULE_RELEASE,
        canonical_surface=canonical_surface,
        candidate_surface=candidate_surface,
        document_id=document_id,
        chunk_id=chunk_id,
        sentence_id=sentence_id,
        canonical_start=c0,
        canonical_end=c1,
        candidate_start=a0,
        candidate_end=a1,
        evidence_text=evidence_text.strip(),
        scope=scope,
        confidence=confidence,
        extractor_release=EXTRACTOR_RELEASE,
        entity_type=entity_type,
    )


def _wrap_schwartz_hearst(
    text: str,
    entities: list[dict[str, Any]],
    *,
    document_id: str,
    chunk_id: str,
) -> list[AliasCandidateV1 | IncompleteAliasCandidate]:
    rows: list[AliasCandidateV1 | IncompleteAliasCandidate] = []
    flat_surfaces: set[str] = set()
    for entity in entities:
        flat_surfaces |= _entity_surfaces(entity)
    if not flat_surfaces:
        return rows
    for match in schwartz_hearst_matches(text):
        short = match["short"]
        long_form = match["long_form"]
        short_l, long_l = short.lower(), long_form.lower()
        if short_l not in flat_surfaces and long_l not in flat_surfaces:
            continue
        # Prefer long form as canonical when either side matches an entity.
        if long_l in flat_surfaces:
            canonical_surface, candidate_surface = long_form, short
            canonical_span = (match["long_start"], match["long_end"])
            candidate_span = (match["short_start"], match["short_end"])
        else:
            canonical_surface, candidate_surface = short, long_form
            canonical_span = (match["short_start"], match["short_end"])
            candidate_span = (match["long_start"], match["long_end"])
        rows.append(
            _emit_or_incomplete(
                candidate_type="acronym_long_form",
                source_method="schwartz_hearst_acronym",
                rule_id="SCHWARTZ_HEARST_V1",
                canonical_surface=canonical_surface,
                candidate_surface=candidate_surface,
                document_id=document_id,
                chunk_id=chunk_id,
                text=text,
                canonical_span=canonical_span,
                candidate_span=candidate_span,
                evidence_text=match["evidence_text"],
                scope="document",
                confidence=0.98,
            )
        )
    return rows


def _wrap_casing_variants(
    text: str,
    entities: list[dict[str, Any]],
    *,
    document_id: str,
    chunk_id: str,
) -> list[AliasCandidateV1 | IncompleteAliasCandidate]:
    rows: list[AliasCandidateV1 | IncompleteAliasCandidate] = []
    for entity in entities:
        canon = norm(entity.get("canonical_name") or entity.get("surface_form") or "")
        if not canon:
            continue
        canon_span = _find_span(text, canon) or _find_span(
            text, norm(entity.get("surface_form") or "")
        )
        for variant in _casing_variants(canon):
            var_span = _find_span(text, variant)
            if var_span is None:
                # Pure normalization not attested in source → incomplete.
                rows.append(
                    _incomplete(
                        reason="casing_variant_not_in_source",
                        source_method="casing_variant",
                        candidate_type="casing_variant",
                        rule_id="CASING_VARIANT_V1",
                        canonical_surface=canon,
                        candidate_surface=variant,
                        document_id=document_id,
                        chunk_id=chunk_id,
                        evidence_text=canon,
                        scope="document",
                        confidence=0.9,
                        canonical_start=None if canon_span is None else canon_span[0],
                        canonical_end=None if canon_span is None else canon_span[1],
                    )
                )
                continue
            rows.append(
                _emit_or_incomplete(
                    candidate_type="casing_variant",
                    source_method="casing_variant",
                    rule_id="CASING_VARIANT_V1",
                    canonical_surface=canon,
                    candidate_surface=text[var_span[0] : var_span[1]],
                    document_id=document_id,
                    chunk_id=chunk_id,
                    text=text,
                    canonical_span=canon_span,
                    candidate_span=var_span,
                    evidence_text=text[
                        min(canon_span[0] if canon_span else var_span[0], var_span[0]) : max(
                            canon_span[1] if canon_span else var_span[1], var_span[1]
                        )
                    ],
                    scope="document",
                    confidence=0.95,
                    entity_type=str(entity.get("entity_type") or "") or None,
                )
            )
    return rows


def _wrap_explicit_lexicon_patterns(
    text: str,
    entities: list[dict[str, Any]],
    *,
    document_id: str,
    chunk_id: str,
) -> list[AliasCandidateV1 | IncompleteAliasCandidate]:
    rows: list[AliasCandidateV1 | IncompleteAliasCandidate] = []
    surfaces = set().union(*(_entity_surfaces(e) for e in entities)) if entities else set()

    for match in _EXPLICIT_ALIAS_RE.finditer(text or ""):
        left = norm(match.group("left"))
        right = norm(match.group("right"))
        if not left or not right:
            continue
        if left.lower() not in surfaces and right.lower() not in surfaces:
            continue
        if left.lower() in surfaces:
            canonical_surface, candidate_surface = left, right
            canonical_span = (match.start("left"), match.end("left"))
            candidate_span = (match.start("right"), match.end("right"))
        else:
            canonical_surface, candidate_surface = right, left
            canonical_span = (match.start("right"), match.end("right"))
            candidate_span = (match.start("left"), match.end("left"))
        rows.append(
            _emit_or_incomplete(
                candidate_type="explicit_alias_pattern",
                source_method="explicit_alias_pattern",
                rule_id="EXPLICIT_ALIAS_PATTERN_V1",
                canonical_surface=canonical_surface,
                candidate_surface=candidate_surface,
                document_id=document_id,
                chunk_id=chunk_id,
                text=text,
                canonical_span=canonical_span,
                candidate_span=candidate_span,
                evidence_text=match.group(0),
                scope="document",
                confidence=1.0,
            )
        )

    for match in _ABBREVIATED_AS_RE.finditer(text or ""):
        long_form = norm(match.group("long"))
        short = norm(match.group("short"))
        if long_form.lower() not in surfaces and short.lower() not in surfaces:
            continue
        rows.append(
            _emit_or_incomplete(
                candidate_type="explicit_abbreviation",
                source_method="explicit_abbreviation",
                rule_id="EXPLICIT_ABBREVIATION_V1",
                canonical_surface=long_form,
                candidate_surface=short,
                document_id=document_id,
                chunk_id=chunk_id,
                text=text,
                canonical_span=(match.start("long"), match.end("long")),
                candidate_span=(match.start("short"), match.end("short")),
                evidence_text=match.group(0),
                scope="document",
                confidence=0.98,
            )
        )

    for match in _FORMERLY_RE.finditer(text or ""):
        current = norm(match.group("current"))
        former = norm(match.group("former"))
        if current.lower() not in surfaces and former.lower() not in surfaces:
            continue
        rows.append(
            _emit_or_incomplete(
                candidate_type="former_name",
                source_method="former_name_pattern",
                rule_id="FORMER_NAME_V1",
                canonical_surface=current,
                candidate_surface=former,
                document_id=document_id,
                chunk_id=chunk_id,
                text=text,
                canonical_span=(match.start("current"), match.end("current")),
                candidate_span=(match.start("former"), match.end("former")),
                evidence_text=match.group(0),
                scope="document",
                confidence=0.95,
            )
        )
    return rows


def _wrap_curated_aliases(
    text: str,
    entities: list[dict[str, Any]],
    *,
    document_id: str,
    chunk_id: str,
) -> list[AliasCandidateV1 | IncompleteAliasCandidate]:
    rows: list[AliasCandidateV1 | IncompleteAliasCandidate] = []
    seen: set[tuple[str, str]] = set()
    for entity in entities:
        surface = norm(entity.get("surface_form") or entity.get("canonical_name") or "")
        if not surface:
            continue
        surface_norm = normalize_entity_name(surface)
        resolved = resolve_entity_alias(surface_norm)
        if not resolved or resolved == surface_norm:
            # Also try canonical_name field.
            canon_field = norm(entity.get("canonical_name") or "")
            if canon_field:
                c_norm = normalize_entity_name(canon_field)
                resolved = resolve_entity_alias(c_norm)
                if not resolved or resolved == c_norm:
                    continue
                surface = canon_field
                surface_norm = c_norm
            else:
                continue
        key = (surface_norm, resolved)
        if key in seen:
            continue
        seen.add(key)
        # Prefer a display form close to the curated target.
        canonical_surface = canonicalize_entity_name(surface)
        # If canonicalize collapses to resolved tokens, keep readable surface from text.
        candidate_span = _find_span(text, surface)
        # Try to locate a longer form matching resolved tokens in text.
        canonical_span = None
        for token_window in (resolved, resolved.replace(" ", "-"), resolved.title()):
            canonical_span = _find_span(text, token_window)
            if canonical_span:
                canonical_surface = text[canonical_span[0] : canonical_span[1]]
                break
        if canonical_span is None or candidate_span is None:
            # Curated hit without dual spans in this chunk → incomplete.
            rows.append(
                _incomplete(
                    reason="curated_alias_missing_dual_spans",
                    source_method="curated_exact_alias",
                    candidate_type="curated_alias",
                    rule_id="CURATED_EXACT_ALIAS_V1",
                    canonical_surface=canonical_surface or resolved,
                    candidate_surface=surface,
                    document_id=document_id,
                    chunk_id=chunk_id,
                    evidence_text=surface,
                    scope="corpus",
                    confidence=1.0,
                    canonical_start=None if canonical_span is None else canonical_span[0],
                    canonical_end=None if canonical_span is None else canonical_span[1],
                    candidate_start=None if candidate_span is None else candidate_span[0],
                    candidate_end=None if candidate_span is None else candidate_span[1],
                )
            )
            continue
        rows.append(
            _emit_or_incomplete(
                candidate_type="curated_alias",
                source_method="curated_exact_alias",
                rule_id="CURATED_EXACT_ALIAS_V1",
                canonical_surface=canonical_surface,
                candidate_surface=surface,
                document_id=document_id,
                chunk_id=chunk_id,
                text=text,
                canonical_span=canonical_span,
                candidate_span=candidate_span,
                evidence_text=text[
                    min(canonical_span[0], candidate_span[0]) : max(
                        canonical_span[1], candidate_span[1]
                    )
                ],
                scope="corpus",
                confidence=1.0,
                entity_type=str(entity.get("entity_type") or "") or None,
            )
        )
    return rows


def _wrap_relex_surface_variants(
    text: str,
    entities: list[dict[str, Any]],
    *,
    document_id: str,
    chunk_id: str,
) -> list[AliasCandidateV1 | IncompleteAliasCandidate]:
    """Wrap consolidated query_aliases / alternate surfaces as retrieval variants.

    These are NOT identity evidence until the gate says otherwise.
    """

    rows: list[AliasCandidateV1 | IncompleteAliasCandidate] = []
    for entity in entities:
        canon = norm(entity.get("canonical_name") or entity.get("surface_form") or "")
        primary = norm(entity.get("surface_form") or canon)
        if not canon:
            continue
        variants: list[str] = []
        for raw in entity.get("query_aliases") or []:
            value = norm(raw)
            if value and value.lower() not in {canon.lower(), primary.lower()}:
                variants.append(value)
        # Also treat a distinct surface_form as a variant of canonical_name.
        if primary and primary.lower() != canon.lower():
            variants.append(primary)
        # Dedup preserving order
        seen: set[str] = set()
        ordered: list[str] = []
        for v in variants:
            k = v.lower()
            if k not in seen:
                seen.add(k)
                ordered.append(v)
        canon_span = _find_span(text, canon) or _find_span(text, primary)
        for variant in ordered:
            var_span = _find_span(text, variant)
            rows.append(
                _emit_or_incomplete(
                    candidate_type="extraction_surface_variant",
                    source_method="extraction_surface_variant",
                    rule_id="RELEX_SURFACE_VARIANT_V1",
                    canonical_surface=canon,
                    candidate_surface=variant,
                    document_id=document_id,
                    chunk_id=chunk_id,
                    text=text,
                    canonical_span=canon_span,
                    candidate_span=var_span,
                    evidence_text=(
                        text[min(canon_span[0], var_span[0]) : max(canon_span[1], var_span[1])]
                        if canon_span and var_span
                        else variant
                    ),
                    scope="document",
                    confidence=float(entity.get("confidence") or 0.5),
                    entity_type=str(entity.get("entity_type") or "") or None,
                )
            )
    return rows


_APPOS_CLASS_TO_CANDIDATE: dict[str, tuple[AliasCandidateType, str, float]] = {
    "explicit_name_alias": ("proper_name_apposition", "spacy_appos_explicit", 0.92),
    "ambiguous_name_like": ("proper_name_apposition", "spacy_appos_ambiguous", 0.55),
    "descriptive_common_noun": ("descriptive_apposition", "spacy_appos_descriptive", 0.9),
    "role": ("role_apposition", "spacy_appos_role", 0.9),
    "location": ("location_apposition", "spacy_appos_location", 0.9),
}


def _wrap_appositions(
    text: str,
    entities: list[dict[str, Any]],
    *,
    document_id: str,
    chunk_id: str,
    doc: Any = None,
) -> list[AliasCandidateV1 | IncompleteAliasCandidate]:
    """Wire typed apposition observations (Phase 3). Never uses length-as-alias."""

    from services.extraction.appos_enrichment import extract_apposition_observations

    rows: list[AliasCandidateV1 | IncompleteAliasCandidate] = []
    try:
        observations = extract_apposition_observations(text, entities, doc=doc)
    except Exception:
        # Fail closed: missing spaCy / parse errors do not invent aliases.
        rows.append(
            _incomplete(
                reason="apposition_extract_failed",
                source_method="spacy_appos",
                document_id=document_id,
                chunk_id=chunk_id,
                evidence_text=(text or "")[:200],
            )
        )
        return rows

    for obs in observations:
        mapping = _APPOS_CLASS_TO_CANDIDATE.get(obs.class_name)
        if mapping is None:
            rows.append(
                _incomplete(
                    reason=f"unknown_apposition_class:{obs.class_name}",
                    source_method="spacy_appos",
                    rule_id=obs.rule_id,
                    canonical_surface=obs.entity_surface or obs.entity_canonical,
                    candidate_surface=obs.appos_text,
                    document_id=document_id,
                    chunk_id=chunk_id,
                    evidence_text=obs.evidence_text,
                )
            )
            continue
        candidate_type, source_method, confidence = mapping
        rows.append(
            _emit_or_incomplete(
                candidate_type=candidate_type,
                source_method=source_method,
                rule_id=obs.rule_id,
                canonical_surface=obs.entity_surface or obs.entity_canonical,
                candidate_surface=obs.appos_text,
                document_id=document_id,
                chunk_id=chunk_id,
                text=text,
                canonical_span=(obs.entity_start, obs.entity_end),
                candidate_span=(obs.appos_start, obs.appos_end),
                evidence_text=obs.evidence_text or obs.sentence_text,
                scope="sentence",
                confidence=confidence,
            )
        )
    return rows


def collect_alias_candidates(
    text: str,
    entities: Iterable[dict[str, Any]] | None,
    *,
    document_id: str,
    chunk_id: str,
    include_curated: bool = True,
    include_relex_surfaces: bool = True,
    include_appositions: bool = True,
    spacy_doc: Any = None,
) -> AliasCandidateBatch:
    """Collect AliasCandidateV1 (+ incomplete) while preserving legacy aliases.

    ``legacy_query_aliases`` is exactly ``extract_aliases(text, entities)`` so
    existing consumers keep bit-compatible string lists.
    """

    entity_rows = [dict(e) for e in (entities or []) if isinstance(e, dict)]
    legacy = extract_aliases(text or "", entity_rows)

    collected: list[AliasCandidateV1 | IncompleteAliasCandidate] = []
    collected.extend(
        _wrap_schwartz_hearst(
            text or "", entity_rows, document_id=document_id, chunk_id=chunk_id
        )
    )
    collected.extend(
        _wrap_explicit_lexicon_patterns(
            text or "", entity_rows, document_id=document_id, chunk_id=chunk_id
        )
    )
    collected.extend(
        _wrap_casing_variants(
            text or "", entity_rows, document_id=document_id, chunk_id=chunk_id
        )
    )
    if include_curated:
        collected.extend(
            _wrap_curated_aliases(
                text or "", entity_rows, document_id=document_id, chunk_id=chunk_id
            )
        )
    if include_relex_surfaces:
        collected.extend(
            _wrap_relex_surface_variants(
                text or "", entity_rows, document_id=document_id, chunk_id=chunk_id
            )
        )
    if include_appositions:
        collected.extend(
            _wrap_appositions(
                text or "",
                entity_rows,
                document_id=document_id,
                chunk_id=chunk_id,
                doc=spacy_doc,
            )
        )

    # Deterministic order for replay: type, rule, spans, surfaces.
    def _sort_key(item: AliasCandidateV1 | IncompleteAliasCandidate) -> tuple:
        if isinstance(item, AliasCandidateV1):
            return (
                0,
                item.candidate_type,
                item.rule_id,
                item.canonical_start,
                item.candidate_start,
                item.canonical_surface.lower(),
                item.candidate_surface.lower(),
                item.alias_candidate_id,
            )
        return (
            1,
            item.candidate_type or "",
            item.rule_id or "",
            item.canonical_start if item.canonical_start is not None else -1,
            item.candidate_start if item.candidate_start is not None else -1,
            item.canonical_surface.lower(),
            item.candidate_surface.lower(),
            item.incomplete_reason,
        )

    collected.sort(key=_sort_key)

    # Deduplicate complete candidates by id; keep first.
    seen_ids: set[str] = set()
    candidates: list[AliasCandidateV1] = []
    incomplete: list[IncompleteAliasCandidate] = []
    for item in collected:
        if isinstance(item, AliasCandidateV1):
            if item.alias_candidate_id in seen_ids:
                continue
            seen_ids.add(item.alias_candidate_id)
            candidates.append(item)
        else:
            incomplete.append(item)

    return AliasCandidateBatch(
        candidates=candidates,
        incomplete=incomplete,
        legacy_query_aliases=legacy,
    )

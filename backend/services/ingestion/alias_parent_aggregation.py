"""Parent-level alias evidence aggregation (Phase 5).

Invariant: multiple child chunks may corroborate, extend, or scope an alias,
but simple parent-level co-occurrence cannot create identity equivalence.
Parent summaries are never identity evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from models.alias_identity import (
    PARENT_ALIAS_BUNDLE_RELEASE,
    AliasCandidateV1,
    AliasDecisionV1,
    ParentAliasBundleV1,
)

BUNDLE_RELEASE = PARENT_ALIAS_BUNDLE_RELEASE

_IDENTITY_DECISIONS = frozenset({"ACCEPT_IDENTITY", "ACCEPT_TEMPORAL_IDENTITY"})
_ACRONYM_TYPES = frozenset({"acronym_long_form", "explicit_abbreviation"})

_BOUNDARY_ACRONYM_RE = re.compile(
    r"(?P<long>[A-Z][A-Za-z0-9][A-Za-z0-9'&\-/]*(?:\s+[A-Z][A-Za-z0-9'&\-/]*){1,8})"
    r"\s*\(\s*(?P<short>[A-Z][A-Z0-9\-]{1,9})\s*\)",
)


@dataclass(frozen=True)
class ChildAliasEvidence:
    """One gated child candidate with parent linkage metadata."""

    candidate: AliasCandidateV1
    decision: AliasDecisionV1
    parent_id: str
    child_id: str
    child_text: str = ""
    child_start_in_parent: int | None = None
    child_end_in_parent: int | None = None


@dataclass(frozen=True)
class ParentSourceRegion:
    """Raw parent source region (never a summary)."""

    parent_id: str
    document_id: str
    parent_text: str
    # Ordered child spans in parent text: (child_id, start, end)
    child_spans: tuple[tuple[str, int, int], ...] = ()


@dataclass(frozen=True)
class ParentAggregationBatch:
    bundles: list[ParentAliasBundleV1] = field(default_factory=list)
    bundle_release: str = BUNDLE_RELEASE
    # Diagnostics for acceptance gates (never used as identity authority).
    cooccurrence_pairs_ignored: int = 0
    boundary_reconstructions: int = 0
    summaries_used_as_identity_evidence: int = 0


def _norm(value: str) -> str:
    return " ".join((value or "").lower().split())


def _short_key(surface: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", surface or "").upper()


def _pair_key(a: str, b: str) -> tuple[str, str]:
    na, nb = _norm(a), _norm(b)
    return (na, nb) if na <= nb else (nb, na)


def _is_acronymish(surface: str) -> bool:
    key = _short_key(surface)
    return 2 <= len(key) <= 10 and (key.isupper() or (surface or "").isupper())


def _children_adjacent_contiguous(
    left: ChildAliasEvidence,
    right: ChildAliasEvidence,
) -> bool:
    if (
        left.child_start_in_parent is None
        or left.child_end_in_parent is None
        or right.child_start_in_parent is None
        or right.child_end_in_parent is None
    ):
        return False
    if left.child_end_in_parent > right.child_start_in_parent:
        return False
    # Contiguous or whitespace-only gap.
    return right.child_start_in_parent - left.child_end_in_parent <= 2


def _surface_mentioned(text: str, surface: str) -> bool:
    if not text or not surface:
        return False
    return _norm(surface) in _norm(text) or surface in text


def aggregate_parent_alias_evidence(
    evidence: Iterable[ChildAliasEvidence] | None,
    *,
    parent_regions: Iterable[ParentSourceRegion] | None = None,
) -> ParentAggregationBatch:
    """Aggregate child-gated evidence into ParentAliasBundleV1 rows.

    Does not call LLMs, read summaries, or invent aliases from co-occurrence.
    """

    rows = list(evidence or [])
    regions = {r.parent_id: r for r in (parent_regions or [])}

    rows.sort(
        key=lambda e: (
            e.candidate.document_id,
            e.parent_id,
            e.child_id,
            e.candidate.candidate_type,
            e.candidate.alias_candidate_id,
        )
    )

    by_parent: dict[tuple[str, str], list[ChildAliasEvidence]] = {}
    for row in rows:
        by_parent.setdefault((row.candidate.document_id, row.parent_id), []).append(row)

    bundles: list[ParentAliasBundleV1] = []
    cooccurrence_ignored = 0
    boundary_count = 0

    for (document_id, parent_id), group in sorted(by_parent.items()):
        bundles.extend(
            _aggregate_one_parent(
                document_id=document_id,
                parent_id=parent_id,
                group=group,
                region=regions.get(parent_id),
            )
        )
        # Count unsafe co-occurrence pairs that were intentionally not merged.
        cooccurrence_ignored += _count_ignored_cooccurrence(group)

    # Boundary reconstruction from raw parent text + contiguous child offsets.
    for region in sorted(regions.values(), key=lambda r: (r.document_id, r.parent_id)):
        rebuilt, n = _reconstruct_boundary_splits(region, by_parent.get(
            (region.document_id, region.parent_id), []
        ))
        bundles.extend(rebuilt)
        boundary_count += n

    # Deterministic order + dedupe by bundle id.
    bundles.sort(
        key=lambda b: (
            b.document_id,
            b.parent_id,
            b.canonical_surface.lower(),
            b.candidate_surface.lower(),
            b.parent_alias_bundle_id,
        )
    )
    seen: set[str] = set()
    unique: list[ParentAliasBundleV1] = []
    for bundle in bundles:
        if bundle.parent_alias_bundle_id in seen:
            continue
        seen.add(bundle.parent_alias_bundle_id)
        unique.append(bundle)

    return ParentAggregationBatch(
        bundles=unique,
        bundle_release=BUNDLE_RELEASE,
        cooccurrence_pairs_ignored=cooccurrence_ignored,
        boundary_reconstructions=boundary_count,
        summaries_used_as_identity_evidence=0,
    )


def _count_ignored_cooccurrence(group: Sequence[ChildAliasEvidence]) -> int:
    """Surfaces that co-occur without shared identity decision are ignored."""

    accepted_pairs = {
        _pair_key(e.candidate.canonical_surface, e.candidate.candidate_surface)
        for e in group
        if e.decision.decision in _IDENTITY_DECISIONS and e.decision.identity_merge_allowed
    }
    surfaces = sorted(
        {
            _norm(e.candidate.canonical_surface)
            for e in group
            if e.candidate.canonical_surface
        }
        | {
            _norm(e.candidate.candidate_surface)
            for e in group
            if e.candidate.candidate_surface
        }
    )
    ignored = 0
    for i, a in enumerate(surfaces):
        for b in surfaces[i + 1 :]:
            if _pair_key(a, b) not in accepted_pairs:
                # Only count when both appear as primary surfaces of different
                # non-identity rows (potential false merge temptation).
                ignored += 1
    return ignored


def _aggregate_one_parent(
    *,
    document_id: str,
    parent_id: str,
    group: Sequence[ChildAliasEvidence],
    region: ParentSourceRegion | None,
) -> list[ParentAliasBundleV1]:
    out: list[ParentAliasBundleV1] = []

    # Index accepted identity evidence by normalized pair.
    identity_rows = [
        e
        for e in group
        if e.decision.decision in _IDENTITY_DECISIONS and e.decision.identity_merge_allowed
    ]

    # Parent/document acronym ambiguity within this parent.
    acronym_long_forms: dict[str, set[str]] = {}
    for e in identity_rows:
        if e.candidate.candidate_type not in _ACRONYM_TYPES:
            continue
        short = e.candidate.candidate_surface
        long_form = e.candidate.canonical_surface
        if len(_norm(long_form)) < len(_norm(short)):
            short, long_form = long_form, short
        acronym_long_forms.setdefault(_short_key(short), set()).add(_norm(long_form))

    ambiguous_shorts = {k for k, vs in acronym_long_forms.items() if len(vs) > 1}

    # Group identity rows by surface pair.
    by_pair: dict[tuple[str, str], list[ChildAliasEvidence]] = {}
    for e in identity_rows:
        by_pair.setdefault(
            _pair_key(e.candidate.canonical_surface, e.candidate.candidate_surface),
            [],
        ).append(e)

    for pair, members in sorted(by_pair.items()):
        members_sorted = sorted(
            members, key=lambda e: (e.child_id, e.candidate.alias_candidate_id)
        )
        head = members_sorted[0]
        short = head.candidate.candidate_surface
        long_form = head.candidate.canonical_surface
        if len(_norm(long_form)) < len(_norm(short)):
            short, long_form = long_form, short

        # Prefer longer form as canonical display.
        canonical = (
            head.candidate.canonical_surface
            if len(_norm(head.candidate.canonical_surface))
            >= len(_norm(head.candidate.candidate_surface))
            else head.candidate.candidate_surface
        )
        alias_surface = (
            head.candidate.candidate_surface
            if canonical == head.candidate.canonical_surface
            else head.candidate.canonical_surface
        )

        short_key = _short_key(alias_surface) if _is_acronymish(alias_surface) else ""
        if short_key and short_key in ambiguous_shorts:
            # Conflicting expansions → REVIEW bundles, no identity merge.
            for e in members_sorted:
                out.append(
                    ParentAliasBundleV1.create(
                        parent_id=parent_id,
                        document_id=document_id,
                        canonical_surface=e.candidate.canonical_surface,
                        candidate_surface=e.candidate.candidate_surface,
                        candidate_type=e.candidate.candidate_type,
                        decision="REVIEW",
                        scope="parent",
                        defining_child_ids=[e.child_id],
                        supporting_child_ids=[e.child_id],
                        evidence_candidate_ids=[e.candidate.alias_candidate_id],
                        ambiguity_status="ambiguous_acronym",
                        identity_merge_allowed=False,
                        entity_type=e.candidate.entity_type,
                    )
                )
            continue

        defining = [e.child_id for e in members_sorted]
        evidence_ids = [e.candidate.alias_candidate_id for e in members_sorted]
        supporting = list(defining)

        # Later usage of the trusted alias/acronym in sibling children.
        if short_key:
            for e in group:
                if e.child_id in supporting:
                    continue
                # Usage evidence: child mentions the short form, and does not
                # assert a conflicting long form via an identity decision.
                if not (
                    _surface_mentioned(e.child_text, alias_surface)
                    or _norm(e.candidate.candidate_surface) == _norm(alias_surface)
                    or _norm(e.candidate.canonical_surface) == _norm(alias_surface)
                ):
                    continue
                if e.decision.decision in _IDENTITY_DECISIONS:
                    other_short = e.candidate.candidate_surface
                    other_long = e.candidate.canonical_surface
                    if len(_norm(other_long)) < len(_norm(other_short)):
                        other_short, other_long = other_long, other_short
                    if _short_key(other_short) == short_key and _norm(other_long) != _norm(
                        canonical
                    ):
                        continue
                supporting.append(e.child_id)

        decision = head.decision.decision
        out.append(
            ParentAliasBundleV1.create(
                parent_id=parent_id,
                document_id=document_id,
                canonical_surface=canonical,
                candidate_surface=alias_surface,
                candidate_type=head.candidate.candidate_type,
                decision=decision,
                scope="parent",
                defining_child_ids=defining,
                supporting_child_ids=supporting,
                evidence_candidate_ids=evidence_ids,
                ambiguity_status="unambiguous_within_parent",
                identity_merge_allowed=True,
                entity_type=head.candidate.entity_type,
            )
        )

    # Retrieval-only / review rows that are NOT identity — pass through as
    # non-merge bundles only when they already have gated non-identity decisions.
    # Do not create identity from co-occurring REVIEW/RETRIEVAL pairs.
    for e in group:
        if e.decision.decision in _IDENTITY_DECISIONS:
            continue
        if e.decision.decision == "ACCEPT_RETRIEVAL_ONLY":
            out.append(
                ParentAliasBundleV1.create(
                    parent_id=parent_id,
                    document_id=document_id,
                    canonical_surface=e.candidate.canonical_surface,
                    candidate_surface=e.candidate.candidate_surface,
                    candidate_type=e.candidate.candidate_type,
                    decision="ACCEPT_RETRIEVAL_ONLY",
                    scope="parent",
                    defining_child_ids=[],
                    supporting_child_ids=[e.child_id],
                    evidence_candidate_ids=[e.candidate.alias_candidate_id],
                    ambiguity_status="not_applicable",
                    identity_merge_allowed=False,
                    entity_type=e.candidate.entity_type,
                )
            )
        elif e.decision.decision == "REVIEW":
            out.append(
                ParentAliasBundleV1.create(
                    parent_id=parent_id,
                    document_id=document_id,
                    canonical_surface=e.candidate.canonical_surface,
                    candidate_surface=e.candidate.candidate_surface,
                    candidate_type=e.candidate.candidate_type,
                    decision="REVIEW",
                    scope="parent",
                    defining_child_ids=[],
                    supporting_child_ids=[e.child_id],
                    evidence_candidate_ids=[e.candidate.alias_candidate_id],
                    ambiguity_status=e.decision.ambiguity_status,
                    identity_merge_allowed=False,
                    entity_type=e.candidate.entity_type,
                )
            )
        # REJECT descriptive/role/location: no identity bundle; clustering
        # may record description_records separately.

    # Silence unused region unless needed for future parent-text checks.
    _ = region
    return out


def _reconstruct_boundary_splits(
    region: ParentSourceRegion,
    group: Sequence[ChildAliasEvidence],
) -> tuple[list[ParentAliasBundleV1], int]:
    """Rebuild acronym patterns split across adjacent contiguous children.

    Requires: adjacent children + contiguous offsets + parent_text proof.
    """

    if not region.parent_text or len(region.child_spans) < 2:
        return [], 0

    spans = sorted(region.child_spans, key=lambda s: (s[1], s[2], s[0]))
    existing_pairs = {
        _pair_key(e.candidate.canonical_surface, e.candidate.candidate_surface)
        for e in group
        if e.decision.decision in _IDENTITY_DECISIONS
    }

    out: list[ParentAliasBundleV1] = []
    count = 0
    for left, right in zip(spans, spans[1:]):
        left_id, left_start, left_end = left
        right_id, right_start, right_end = right
        if right_start < left_end:
            continue
        if right_start - left_end > 2:
            continue
        # Window must be proven in parent text, not inferred from child strings alone.
        window_start = max(0, left_start)
        window_end = min(len(region.parent_text), right_end)
        window = region.parent_text[window_start:window_end]
        match = _BOUNDARY_ACRONYM_RE.search(window)
        if not match:
            continue
        long_form = match.group("long").strip()
        short = match.group("short").strip()
        if _pair_key(long_form, short) in existing_pairs:
            continue
        # Absolute match offsets must land across the boundary.
        abs_long_start = window_start + match.start("long")
        abs_short_end = window_start + match.end("short")
        if not (abs_long_start < left_end and abs_short_end > right_start):
            # Pattern must straddle the child boundary.
            if not (
                left_start <= abs_long_start < left_end
                and right_start < abs_short_end <= right_end
            ):
                continue
        out.append(
            ParentAliasBundleV1.create(
                parent_id=region.parent_id,
                document_id=region.document_id,
                canonical_surface=long_form,
                candidate_surface=short,
                candidate_type="acronym_long_form",
                decision="ACCEPT_IDENTITY",
                scope="parent",
                defining_child_ids=[left_id, right_id],
                supporting_child_ids=[left_id, right_id],
                evidence_candidate_ids=[],
                ambiguity_status="unambiguous_within_parent",
                identity_merge_allowed=True,
                reconstruction_method="contiguous_parent_offset_boundary_v1",
            )
        )
        count += 1
    return out, count

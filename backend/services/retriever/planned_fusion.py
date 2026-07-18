"""Rank-only fusion for QueryPlanV2 candidate lanes."""

from __future__ import annotations

import re
from dataclasses import dataclass

from models.schemas import SourceChunk
from services.retriever.evidence_allocation import allocate_two_lane_seats
from services.retriever.planned_attribution import merge_planned_attribution
from services.retriever.query_plan import FALLBACK_PROBE_ID
from services.retriever.reservation_policy import (
    CORPUS_RESERVATION_MIN_SCORE,
    CORPUS_RESERVATION_MIN_SCORE_RATIO,
    corpus_reservation_bound,
    passes_corpus_reservation,
)


@dataclass(frozen=True)
class PlannedPool:
    lane_id: str
    retriever: str
    chunks: tuple[SourceChunk, ...]
    required: bool = False
    anchor_phrase: str | None = None
    anchor_phrases: tuple[str, ...] = ()
    anchor_terms: tuple[str, ...] = ()


LANE_GROUNDING_THRESHOLD = 0.75
DOCUMENT_ROUTE_GROUNDING_THRESHOLD = 0.30
LANE_RESERVATION_MIN_SCORE_RATIO = 0.25
_OPERATIONAL_ARTIFACT_RE = re.compile(
    r"^(?:ocr-(?:completion|marker-append)|epub-backfill-status)-report(?:\.[a-z0-9]+)?$",
    re.IGNORECASE,
)
_TRAILING_DESCRIPTORS = frozenset(
    {
        "framework",
        "mechanism",
        "method",
        "model",
        "positioning",
        "principles",
        "strategy",
    }
)


def _normalized_tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(value or "").lower())


def is_operational_artifact_source(chunk: SourceChunk) -> bool:
    """Exclude deterministic pipeline reports that are not corpus evidence."""

    name = str(chunk.doc_name or "").strip().rsplit("/", 1)[-1]
    return bool(_OPERATIONAL_ARTIFACT_RE.fullmatch(name))


def _lane_grounding_score(chunk: SourceChunk, pool: PlannedPool) -> float:
    """Measure whether a lane candidate actually contains its named concept."""
    haystack = " ".join(
        [
            str(chunk.doc_name or ""),
            " ".join(str(item) for item in (chunk.heading_path or [])),
            str(chunk.text or ""),
            str(chunk.summary or ""),
            " ".join(
                str(item.get(key) or "")
                for item in (chunk.provenance or [])
                if isinstance(item, dict)
                for key in (
                    "entity",
                    "entity_id",
                    "predicate",
                    "relation_family",
                    "bridge_type",
                    "evidence_phrase",
                )
            ),
        ]
    )
    haystack_tokens = _normalized_tokens(haystack)
    if not haystack_tokens:
        return 0.0
    haystack_text = " ".join(haystack_tokens)

    phrase_variants: list[list[str]] = []
    for phrase in (pool.anchor_phrase, *pool.anchor_phrases):
        phrase_tokens = _normalized_tokens(phrase or "")
        if not phrase_tokens or phrase_tokens in phrase_variants:
            continue
        phrase_variants.append(phrase_tokens)
        if len(phrase_tokens) >= 3 and phrase_tokens[-1] in _TRAILING_DESCRIPTORS:
            phrase_variants.append(phrase_tokens[:-1])
    exact_phrase = any(
        len(tokens) >= 2 and " ".join(tokens) in haystack_text
        for tokens in phrase_variants
    )

    anchor_terms = list(dict.fromkeys(_normalized_tokens(" ".join(pool.anchor_terms))))
    if not anchor_terms:
        anchor_terms = phrase_variants[0] if phrase_variants else []
    haystack_set = set(haystack_tokens)
    coverage = (
        len(set(anchor_terms) & haystack_set) / len(anchor_terms)
        if anchor_terms
        else 0.0
    )
    primary_phrase_tokens = _normalized_tokens(pool.anchor_phrase or "")
    if (
        len(primary_phrase_tokens) == 2
        or any(token.isdigit() for token in primary_phrase_tokens)
    ) and not exact_phrase:
        # Separated common words in a long chunk do not establish the
        # multiword concept. Require adjacency or a curated multiword alias.
        coverage *= 0.5
    return round(2.0 + coverage, 4) if exact_phrase else round(coverage, 4)


def planned_lane_grounding(chunk: SourceChunk, lane_id: str) -> float:
    values = (chunk.metadata or {}).get("planned_lane_grounding") or {}
    if not isinstance(values, dict):
        return 0.0
    try:
        return float(values.get(lane_id) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def planned_document_route_score(chunk: SourceChunk, lane_id: str) -> float:
    values = (chunk.metadata or {}).get("document_route_lanes") or {}
    if not isinstance(values, dict):
        return 0.0
    try:
        return float(values.get(lane_id) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def planned_lane_supported(chunk: SourceChunk, lane_id: str) -> bool:
    """Accept literal grounding or evidence descended from a semantic route.

    Route support is deliberately conjunctive: a chunk must have been
    retrieved for the lane and must belong to a document whose source profile
    cleared the semantic routing floor. A routed profile alone is never final
    evidence; the candidate still has to be a descended parent/child passage.
    """

    if planned_lane_grounding(chunk, lane_id) >= LANE_GROUNDING_THRESHOLD:
        return True
    planned_lanes = set((chunk.metadata or {}).get("planned_lanes") or [])
    return bool(
        lane_id in planned_lanes
        and planned_document_route_score(chunk, lane_id)
        >= DOCUMENT_ROUTE_GROUNDING_THRESHOLD
    )


def reserved_required_lane_ids(
    chunk: SourceChunk,
    required_lane_ids: list[str] | None = None,
) -> list[str]:
    """Return required lanes explicitly reserved for this final source.

    Semantic routing can annotate many neighboring candidates. Only the one
    selected as the final reservation for a required lane should bypass a
    later context filter.
    """

    required = set(required_lane_ids or [])
    reserved = list(
        (chunk.metadata or {}).get("planned_required_lane_reservations") or []
    )
    return [
        lane_id
        for lane_id in dict.fromkeys(str(value) for value in reserved if value)
        if lane_id in required and planned_lane_supported(chunk, lane_id)
    ]


def annotate_planned_lane_grounding(
    chunks: list[SourceChunk],
    *,
    lane_id: str,
    anchor_phrase: str | None,
    anchor_phrases: tuple[str, ...] = (),
    anchor_terms: tuple[str, ...] = (),
) -> None:
    """Attach grounding diagnostics without changing candidate scores/order."""
    pool = PlannedPool(
        lane_id=lane_id,
        retriever="grounding",
        chunks=(),
        anchor_phrase=anchor_phrase,
        anchor_phrases=anchor_phrases,
        anchor_terms=anchor_terms,
    )
    for chunk in chunks:
        metadata = dict(chunk.metadata or {})
        planned_lanes = set(metadata.get("planned_lanes") or [])
        planned_lanes.add(lane_id)
        metadata["planned_lanes"] = sorted(planned_lanes)
        grounding = dict(metadata.get("planned_lane_grounding") or {})
        grounding[lane_id] = max(
            float(grounding.get(lane_id) or 0.0),
            _lane_grounding_score(chunk, pool),
        )
        metadata["planned_lane_grounding"] = grounding
        chunk.metadata = metadata


def propagate_grounded_lane_aliases(
    chunks: list[SourceChunk],
    lane_targets: dict[str, list[str]],
) -> dict[str, object]:
    """Credit required lanes only from literally grounded translation lanes.

    The vocabulary resolver supplies the source-to-target mapping. This helper
    performs no semantic guessing: the source lane must already be present on
    the candidate and its literal concept grounding must clear the normal lane
    threshold. Route-only and step-back evidence therefore cannot inherit a
    required obligation.
    """

    diagnostics: dict[str, object] = {
        "source_lane_count": len(lane_targets),
        "candidate_count": len(chunks),
        "propagated_candidate_count": 0,
        "propagated_pairs": [],
    }
    propagated_candidates = 0
    propagated_pairs: set[tuple[str, str]] = set()
    for chunk in chunks:
        metadata = dict(chunk.metadata or {})
        planned_lanes = set(metadata.get("planned_lanes") or [])
        grounding = dict(metadata.get("planned_lane_grounding") or {})
        grounding_sources = {
            str(lane_id): list(values or [])
            for lane_id, values in (
                metadata.get("planned_lane_grounding_sources") or {}
            ).items()
        }
        changed = False
        for source_lane_id, target_lane_ids in lane_targets.items():
            if source_lane_id not in planned_lanes:
                continue
            try:
                source_score = float(grounding.get(source_lane_id) or 0.0)
            except (TypeError, ValueError):
                source_score = 0.0
            if source_score < LANE_GROUNDING_THRESHOLD:
                continue
            for target_lane_id in target_lane_ids:
                target = str(target_lane_id or "").strip()
                if not target or target == source_lane_id:
                    continue
                planned_lanes.add(target)
                try:
                    previous_score = float(grounding.get(target) or 0.0)
                except (TypeError, ValueError):
                    previous_score = 0.0
                grounding[target] = max(previous_score, source_score)
                sources = grounding_sources.setdefault(target, [])
                if source_lane_id not in sources:
                    sources.append(source_lane_id)
                propagated_pairs.add((source_lane_id, target))
                changed = True
        if not changed:
            continue
        metadata["planned_lanes"] = sorted(planned_lanes)
        metadata["planned_lane_grounding"] = grounding
        metadata["planned_lane_grounding_sources"] = {
            lane_id: sorted(dict.fromkeys(values))
            for lane_id, values in grounding_sources.items()
        }
        chunk.metadata = metadata
        propagated_candidates += 1
    diagnostics["propagated_candidate_count"] = propagated_candidates
    diagnostics["propagated_pairs"] = [
        {"source_lane_id": source, "target_lane_id": target}
        for source, target in sorted(propagated_pairs)
    ]
    return diagnostics


def grounded_planned_lane_ids(
    chunks: list[SourceChunk],
    required_lane_ids: list[str],
) -> list[str]:
    """Return required lanes backed by grounded evidence, not provenance alone."""
    return sorted(
        lane_id
        for lane_id in dict.fromkeys(required_lane_ids)
        if any(planned_lane_supported(chunk, lane_id) for chunk in chunks)
    )


def filter_grounded_planned_candidates(
    chunks: list[SourceChunk],
    required_lane_ids: list[str],
    *,
    selected_corpus_ids: list[str] | None = None,
    protected_lane_ids: list[str] | None = None,
) -> tuple[list[SourceChunk], dict[str, object]]:
    """Remove generic quota fillers from a fully grounded multi-side pack.

    The filter is intentionally conditional. It activates only when at least
    two semantic sides are required and grounded evidence for every side is
    already present. Graph relation/bridge candidates remain eligible when
    their text or structured provenance grounds at least one required side.
    """
    required = list(dict.fromkeys(required_lane_ids))
    protected = list(dict.fromkeys(protected_lane_ids or []))
    diagnostics: dict[str, object] = {
        "enabled": bool(required),
        "input": len(chunks),
        "kept": len(chunks),
        "dropped": 0,
        "required_lane_ids": required,
        "protected_lane_ids": protected,
        "grounded_lane_ids": grounded_planned_lane_ids(chunks, required),
        "applied": False,
    }
    # The synthetic fallback probe is retrieval plumbing, not a semantic side.
    # Gating a whole packet on its 0.75 grounding threshold starves broad
    # undecomposed queries and produced false refusals (P0.4). Named
    # single-side filtering (a real user concept) remains supported.
    synthetic_dropped = [lane for lane in required if lane == FALLBACK_PROBE_ID]
    if synthetic_dropped:
        required = [lane for lane in required if lane != FALLBACK_PROBE_ID]
        diagnostics["required_lane_ids"] = required
        diagnostics["synthetic_lanes_excluded"] = synthetic_dropped
        diagnostics["grounded_lane_ids"] = grounded_planned_lane_ids(chunks, required)
    if not required:
        diagnostics["reason"] = (
            "synthetic_fallback_lane_fail_open"
            if synthetic_dropped
            else "no_required_lanes"
        )
        return chunks, diagnostics
    if any(
        len(_normalized_tokens(lane_id)) == 1 and len(lane_id) <= 3
        for lane_id in required
    ):
        diagnostics["reason"] = "short_acronym_lane_fail_open"
        return chunks, diagnostics
    grounded_lane_ids = list(diagnostics["grounded_lane_ids"])
    if not grounded_lane_ids:
        diagnostics["reason"] = "no_grounded_coverage"
        return chunks, diagnostics

    def grounded_translation_lanes(chunk: SourceChunk) -> list[str]:
        grounding = (chunk.metadata or {}).get("planned_lane_grounding") or {}
        if not isinstance(grounding, dict):
            return []
        output: list[str] = []
        for lane_id, value in grounding.items():
            source_lane_id = str(lane_id or "")
            if (
                not (
                    source_lane_id.startswith("translation_")
                    or source_lane_id.startswith("planner_translation_")
                )
                or source_lane_id in required
            ):
                continue
            try:
                score = float(value or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            if score >= LANE_GROUNDING_THRESHOLD:
                output.append(source_lane_id)
        return sorted(output)

    grounded_translation_candidates = {
        id(chunk): grounded_translation_lanes(chunk) for chunk in chunks
    }
    protected_lane_candidates = {
        id(chunk): sorted(
            set((chunk.metadata or {}).get("planned_lanes") or []) & set(protected)
        )
        for chunk in chunks
    }
    filtered = [
        chunk
        for chunk in chunks
        if any(planned_lane_supported(chunk, lane_id) for lane_id in grounded_lane_ids)
        or bool(grounded_translation_candidates[id(chunk)])
        or bool(protected_lane_candidates[id(chunk)])
    ]
    if not filtered:
        diagnostics["reason"] = "empty_filter_guard"
        return chunks, diagnostics

    preserved_corpora: list[str] = []
    skipped_preservations: list[dict[str, object]] = []
    requested_corpora = list(
        dict.fromkeys(str(value) for value in (selected_corpus_ids or []) if str(value))
    )
    if len(requested_corpora) > 1:
        kept_ids = {id(chunk) for chunk in filtered}
        represented = {str(chunk.corpus_id or "") for chunk in filtered}
        # Preservation is a corpus seat, so it must pass the same calibrated
        # reservation bound as every other corpus seat (P0.3). Pick the
        # corpus's best-scoring candidate, not its first in input order.
        top_score = max((float(chunk.score or 0.0) for chunk in chunks), default=0.0)
        for corpus_id in requested_corpora:
            if corpus_id in represented:
                continue
            corpus_chunks = [
                chunk for chunk in chunks if str(chunk.corpus_id or "") == corpus_id
            ]
            if not corpus_chunks:
                skipped_preservations.append(
                    {"corpus_id": corpus_id, "reason": "no_candidate"}
                )
                continue
            candidate = max(corpus_chunks, key=lambda c: float(c.score or 0.0))
            candidate_score = float(candidate.score or 0.0)
            if not passes_corpus_reservation(candidate_score, top_score):
                skipped_preservations.append(
                    {
                        "corpus_id": corpus_id,
                        "reason": "below_reservation_bound",
                        "best_score": round(candidate_score, 4),
                        "bound": corpus_reservation_bound(top_score),
                    }
                )
                continue
            kept_ids.add(id(candidate))
            represented.add(corpus_id)
            preserved_corpora.append(corpus_id)
        filtered = [chunk for chunk in chunks if id(chunk) in kept_ids]

    diagnostics.update(
        {
            "applied": True,
            "reason": (
                "all_required_sides_grounded"
                if set(grounded_lane_ids) == set(required)
                else "partial_grounded_coverage"
            ),
            "kept": len(filtered),
            "dropped": len(chunks) - len(filtered),
            "corpus_floor_candidates_preserved": preserved_corpora,
            "corpus_floor_candidates_skipped": skipped_preservations,
            "grounded_translation_candidates_preserved": sum(
                bool(grounded_translation_candidates.get(id(chunk)))
                for chunk in filtered
            ),
            "protected_lane_candidates_preserved": sum(
                bool(protected_lane_candidates.get(id(chunk))) for chunk in filtered
            ),
        }
    )
    return filtered, diagnostics


def _candidate_key(chunk: SourceChunk) -> str:
    content_id = str(chunk.chunk_id or chunk.parent_id or "")
    if not content_id:
        return ""
    return f"{str(chunk.corpus_id or '')}|{content_id}"


def _candidate_document_key(chunk: SourceChunk) -> str:
    metadata = chunk.metadata or {}
    document_id = (
        str(chunk.doc_id or metadata.get("source_file_hash") or chunk.doc_name or "")
        .strip()
        .lower()
    )
    if not document_id:
        return ""
    return f"{str(chunk.corpus_id or '')}|{document_id}"


def limit_candidates_per_document(
    chunks: list[SourceChunk],
    *,
    max_candidates: int,
    max_per_document: int,
    required_lane_ids: list[str] | None = None,
    preferred_route_lane_ids: list[str] | None = None,
    protected_lane_ids: list[str] | None = None,
) -> tuple[list[SourceChunk], int]:
    """Bound same-document neighbors before the cross-encoder call.

    Input order is preserved. Candidates without a durable document identity
    are retained because dropping unknown identities would trade recall for an
    ingestion metadata gap.
    """

    limit = max(1, int(max_candidates))
    document_limit = max(1, int(max_per_document))
    required = list(dict.fromkeys(required_lane_ids or []))
    remaining_required = set(required)
    protected_object_ids: set[int] = set()
    for lane_id in dict.fromkeys(protected_lane_ids or []):
        selected_chunk = next(
            (
                chunk
                for chunk in chunks
                if lane_id in set((chunk.metadata or {}).get("planned_lanes") or [])
            ),
            None,
        )
        if selected_chunk is not None and len(protected_object_ids) < limit:
            protected_object_ids.add(id(selected_chunk))
    for lane_id in dict.fromkeys(preferred_route_lane_ids or []):
        routed = [
            (rank, chunk)
            for rank, chunk in enumerate(chunks)
            if planned_lane_supported(chunk, lane_id)
            and planned_document_route_score(chunk, lane_id) > 0.0
        ]
        if not routed or len(protected_object_ids) >= limit:
            continue
        _rank, selected_chunk = max(
            routed,
            key=lambda row: (
                planned_document_route_score(row[1], lane_id),
                planned_lane_grounding(row[1], lane_id),
                float(row[1].score or 0.0),
                -row[0],
            ),
        )
        protected_object_ids.add(id(selected_chunk))
        supported_required = {
            lane
            for lane in list(remaining_required)
            if planned_lane_supported(selected_chunk, lane)
        }
        remaining_required.difference_update(supported_required)
    while remaining_required and len(protected_object_ids) < limit:
        choices: list[tuple[tuple[float, ...], SourceChunk, set[str]]] = []
        for rank, chunk in enumerate(chunks):
            supported = {
                lane_id
                for lane_id in remaining_required
                if planned_lane_supported(chunk, lane_id)
            }
            if not supported:
                continue
            choices.append(
                (
                    (
                        float(len(supported)),
                        max(
                            (
                                planned_lane_grounding(chunk, lane_id)
                                for lane_id in supported
                            ),
                            default=0.0,
                        ),
                        max(
                            (
                                planned_document_route_score(chunk, lane_id)
                                for lane_id in supported
                            ),
                            default=0.0,
                        ),
                        float(chunk.score or 0.0),
                        float(-rank),
                    ),
                    chunk,
                    supported,
                )
            )
        if not choices:
            break
        _priority, selected_chunk, supported = max(
            choices,
            key=lambda row: row[0],
        )
        protected_object_ids.add(id(selected_chunk))
        remaining_required.difference_update(supported)

    output: list[SourceChunk] = []
    document_counts: dict[str, int] = {}
    for chunk in chunks:
        if id(chunk) not in protected_object_ids:
            continue
        document_key = _candidate_document_key(chunk)
        if document_key:
            document_counts[document_key] = document_counts.get(document_key, 0) + 1
    protected_remaining = len(protected_object_ids)
    dropped = 0
    for chunk in chunks:
        if len(output) >= limit:
            break
        protected = id(chunk) in protected_object_ids
        if protected:
            output.append(chunk)
            protected_remaining -= 1
            continue
        if len(output) + protected_remaining >= limit:
            dropped += 1
            continue
        document_key = _candidate_document_key(chunk)
        if document_key and document_counts.get(document_key, 0) >= document_limit:
            dropped += 1
            continue
        output.append(chunk)
        if document_key:
            document_counts[document_key] = document_counts.get(document_key, 0) + 1
    return output, dropped


def _has_lane_retriever(chunk: SourceChunk, lane_id: str, retriever: str) -> bool:
    expected = f"planned_{retriever}"
    return any(
        isinstance(item, dict)
        and str(item.get("lane_id") or "") == lane_id
        and str(item.get("retriever") or "") == expected
        for item in (chunk.provenance or [])
    )


def _bounded_lane_seat_quotas(
    required_lane_ids: list[str],
    lane_seat_quotas: dict[str, int] | None,
    *,
    limit: int,
) -> tuple[dict[str, int], dict[str, int]]:
    """Fit requested quotas into the packet without starving later lanes."""

    lanes = list(dict.fromkeys(required_lane_ids))
    requested = {
        lane_id: max(1, int((lane_seat_quotas or {}).get(lane_id, 1)))
        for lane_id in lanes
    }
    remaining = max(0, int(limit))
    effective = {lane_id: 0 for lane_id in lanes}
    for lane_id in lanes:
        if remaining <= 0:
            break
        effective[lane_id] = 1
        remaining -= 1
    while remaining > 0:
        progressed = False
        for lane_id in lanes:
            if remaining <= 0:
                break
            if effective[lane_id] >= requested[lane_id]:
                continue
            effective[lane_id] += 1
            remaining -= 1
            progressed = True
        if not progressed:
            break
    return requested, effective


def cap_planned_candidates_by_affinity(
    chunks: list[SourceChunk],
    *,
    lane_rerank_caps: dict[str, int],
    global_rerank_cap: int,
) -> tuple[list[SourceChunk], dict[str, object]]:
    """Cap the deduplicated union by each candidate's max-affinity lane."""

    lane_ids = list(dict.fromkeys(lane_rerank_caps))
    requested, effective = _bounded_lane_seat_quotas(
        lane_ids,
        lane_rerank_caps,
        limit=max(1, int(global_rerank_cap)),
    )
    union_limit = max(
        1,
        min(
            max(1, int(global_rerank_cap)),
            max(1, sum(effective.values())),
        ),
    )
    counts = {lane_id: 0 for lane_id in lane_ids}
    dropped = {lane_id: 0 for lane_id in lane_ids}
    selected_keys: set[str] = set()

    def select(chunk: SourceChunk) -> bool:
        key = _candidate_key(chunk)
        if not key or key in selected_keys or len(selected_keys) >= union_limit:
            return False
        lane_id = str((chunk.metadata or {}).get("planned_max_affinity_lane") or "")
        if lane_id in effective and counts[lane_id] >= effective[lane_id]:
            dropped[lane_id] += 1
            return False
        selected_keys.add(key)
        if lane_id in effective:
            counts[lane_id] += 1
        return True

    original = next(
        (
            chunk
            for chunk in chunks
            if "original" in set((chunk.metadata or {}).get("planned_lanes") or [])
        ),
        None,
    )
    if original is not None:
        select(original)
    for lane_id in lane_ids:
        candidate = next(
            (
                chunk
                for chunk in chunks
                if str((chunk.metadata or {}).get("planned_max_affinity_lane") or "")
                == lane_id
            ),
            None,
        )
        if candidate is not None:
            select(candidate)
    for chunk in chunks:
        select(chunk)
        if len(selected_keys) >= union_limit:
            break
    output = [chunk for chunk in chunks if _candidate_key(chunk) in selected_keys]
    return output, {
        "active": bool(lane_ids),
        "requested": requested,
        "effective": effective,
        "requested_sum": sum(requested.values()),
        "effective_sum": sum(effective.values()),
        "global_rerank_cap": max(1, int(global_rerank_cap)),
        "union_limit": union_limit,
        "input_candidates": len(chunks),
        "output_candidates": len(output),
        "assigned_counts": counts,
        "dropped_by_lane_cap": dropped,
        "affinity_authority": ("literal_grounding_or_document_route_no_score_mutation"),
    }


def _reconcile_lane_quota_receipt(
    output: list[SourceChunk],
    receipt: dict[str, object],
    *,
    requested_quotas: dict[str, int],
    effective_quotas: dict[str, int],
) -> dict[str, object]:
    """Make quota telemetry describe the returned packet, never pre-trim state."""

    reservations: dict[str, list[str]] = {lane_id: [] for lane_id in requested_quotas}
    reservation_refs: dict[str, list[str]] = {
        lane_id: [] for lane_id in requested_quotas
    }
    corpus_reservations: dict[str, str] = {}
    corpus_reservation_refs: dict[str, str] = {}
    protected_lane_reservations: dict[str, str] = {}
    protected_lane_reservation_refs: dict[str, str] = {}
    for chunk in output:
        key = _candidate_key(chunk)
        public_id = str(chunk.chunk_id or chunk.parent_id or "")
        for lane_id in dict.fromkeys(
            (chunk.metadata or {}).get("planned_required_lane_reservations") or []
        ):
            if lane_id not in reservations:
                continue
            reservations[lane_id].append(public_id)
            reservation_refs[lane_id].append(key)
        for corpus_id in dict.fromkeys(
            (chunk.metadata or {}).get("planned_corpus_reservations") or []
        ):
            corpus_reservations[str(corpus_id)] = public_id
            corpus_reservation_refs[str(corpus_id)] = key
        for lane_id in dict.fromkeys(
            (chunk.metadata or {}).get("planned_protected_lane_reservations") or []
        ):
            protected_lane_reservations[str(lane_id)] = public_id
            protected_lane_reservation_refs[str(lane_id)] = key
    previously_reserved_corpora = {
        str(corpus_id) for corpus_id in (receipt.get("corpus_reservations") or {})
    }
    displaced_corpora = sorted(previously_reserved_corpora - set(corpus_reservations))
    skipped_corpora = list(receipt.get("skipped_corpus_reservations") or [])
    skipped_corpora.extend(
        corpus_id for corpus_id in displaced_corpora if corpus_id not in skipped_corpora
    )
    corpus_details = {
        str(corpus_id): dict(value)
        for corpus_id, value in (
            receipt.get("corpus_reservation_details") or {}
        ).items()
        if isinstance(value, dict)
    }
    for corpus_id in displaced_corpora:
        corpus_details.setdefault(corpus_id, {})[
            "outcome"
        ] = "displaced_by_librarian_seat_budget"
    previously_protected_lanes = {
        str(lane_id) for lane_id in (receipt.get("protected_lane_reservations") or {})
    }
    displaced_protected_lanes = sorted(
        previously_protected_lanes - set(protected_lane_reservations)
    )
    reconciled = dict(receipt)
    reconciled.update(
        {
            "lane_seat_quotas_requested": dict(requested_quotas),
            "lane_seat_quotas": dict(effective_quotas),
            "lane_quota_reservations": reservations,
            "lane_quota_reservation_refs": reservation_refs,
            "lane_reservations": {
                lane_id: values[0] for lane_id, values in reservations.items() if values
            },
            "lane_reservation_refs": {
                lane_id: values[0]
                for lane_id, values in reservation_refs.items()
                if values
            },
            "lane_quota_fulfilled": {
                lane_id: len(reservations[lane_id]) for lane_id in requested_quotas
            },
            "lane_quota_spillover": {
                lane_id: max(
                    0,
                    requested_quotas[lane_id] - len(reservations[lane_id]),
                )
                for lane_id in requested_quotas
            },
            "corpus_reservations": corpus_reservations,
            "corpus_reservation_refs": corpus_reservation_refs,
            "skipped_corpus_reservations": skipped_corpora,
            "corpus_reservation_details": corpus_details,
            "protected_lane_reservations": protected_lane_reservations,
            "protected_lane_reservation_refs": protected_lane_reservation_refs,
            "displaced_protected_lane_ids": displaced_protected_lanes,
            "selected_candidates": len(output),
        }
    )
    return reconciled


def apply_librarian_two_lane_allocation(
    selected: list[SourceChunk],
    ranked: list[SourceChunk],
    *,
    query: str,
    required_lane_ids: list[str],
    lane_seat_quotas: dict[str, int],
    reservation_receipt: dict[str, object],
    anchor_ratio: float,
    anchor_threshold: float,
    expansion_threshold: float,
) -> tuple[list[SourceChunk], dict[str, object], dict[str, object]]:
    """Apply anchor/expansion allocation inside each librarian subquery."""

    required = list(dict.fromkeys(required_lane_ids))
    lane_order = {lane_id: index for index, lane_id in enumerate(required)}
    selected_keys = {_candidate_key(candidate) for candidate in selected}

    def candidate_lane(candidate: SourceChunk) -> str:
        reserved = [
            lane_id
            for lane_id in (
                (candidate.metadata or {}).get("planned_required_lane_reservations")
                or []
            )
            if lane_id in lane_order
        ]
        if reserved:
            return min(reserved, key=lane_order.__getitem__)
        if _candidate_key(candidate) in selected_keys:
            return "__unassigned__"
        supported = [
            lane_id
            for lane_id in required
            if planned_lane_supported(candidate, lane_id)
        ]
        if not supported:
            return "__unassigned__"
        return max(
            supported,
            key=lambda lane_id: (
                planned_lane_grounding(candidate, lane_id),
                planned_document_route_score(candidate, lane_id),
                -lane_order[lane_id],
            ),
        )

    protected_ids = {
        _candidate_key(candidate)
        for candidate in selected
        if (candidate.metadata or {}).get("planned_corpus_reservations")
        or (candidate.metadata or {}).get("planned_protected_lane_reservations")
    }
    allocation = allocate_two_lane_seats(
        selected,
        ranked,
        query=query,
        budget=len(selected),
        anchor_ratio=anchor_ratio,
        anchor_threshold=anchor_threshold,
        expansion_threshold=expansion_threshold,
        candidate_id_fn=_candidate_key,
        score_fn=lambda candidate: float(candidate.score or 0.0),
        side_fn=candidate_lane,
        protected_ids=protected_ids,
    )
    output: list[SourceChunk] = []
    for candidate in allocation.candidates:
        copied = candidate.model_copy(deep=True)
        lane_id = candidate_lane(copied)
        metadata = dict(copied.metadata or {})
        if lane_id in lane_order:
            metadata["planned_required_lane_reservations"] = [lane_id]
        else:
            metadata.pop("planned_required_lane_reservations", None)
        copied.metadata = metadata
        output.append(copied)
    requested = dict(
        reservation_receipt.get("lane_seat_quotas_requested") or lane_seat_quotas
    )
    effective = dict(reservation_receipt.get("lane_seat_quotas") or lane_seat_quotas)
    reconciled = _reconcile_lane_quota_receipt(
        output,
        reservation_receipt,
        requested_quotas=requested,
        effective_quotas=effective,
    )
    diagnostics = dict(allocation.diagnostics)
    diagnostics["scope"] = "within_librarian_subquery_seats"
    diagnostics["required_lane_ids"] = required
    return output, diagnostics, reconciled


def fuse_planned_pools(
    pools: list[PlannedPool],
    *,
    max_candidates: int,
    corpus_ids: list[str] | None = None,
    rrf_k: float = 60.0,
) -> tuple[list[SourceChunk], dict[str, object]]:
    """Fuse lane-local ranks with required-lane and corpus reservations."""

    max_candidates = max(1, int(max_candidates))
    scores: dict[str, float] = {}
    representatives: dict[str, SourceChunk] = {}
    lane_keys: dict[str, list[str]] = {}
    retriever_counts: dict[str, int] = {}

    retriever_weights = {"dense": 1.0, "summary": 0.75, "lexical": 0.85, "graph": 0.9}
    excluded_operational_artifacts = 0
    for pool in pools:
        weight = retriever_weights.get(pool.retriever, 0.8)
        retriever_counts[pool.retriever] = retriever_counts.get(
            pool.retriever, 0
        ) + len(pool.chunks)
        lane_bucket = lane_keys.setdefault(pool.lane_id, [])
        for rank, chunk in enumerate(pool.chunks):
            if is_operational_artifact_source(chunk):
                excluded_operational_artifacts += 1
                continue
            key = _candidate_key(chunk)
            if not key:
                continue
            if key not in lane_bucket:
                lane_bucket.append(key)
            scores[key] = scores.get(key, 0.0) + weight / (rrf_k + rank + 1.0)
            existing = representatives.get(key)
            previous_metadata = dict(existing.metadata or {}) if existing else {}
            previous_provenance = list(existing.provenance or []) if existing else []
            if existing is None or float(chunk.score or 0.0) > float(
                existing.score or 0.0
            ):
                representatives[key] = chunk.model_copy(deep=True)
            representative = representatives[key]
            metadata = merge_planned_attribution(
                previous_metadata,
                dict(chunk.metadata or {}),
            )
            metadata = merge_planned_attribution(
                metadata,
                dict(representative.metadata or {}),
            )
            planned_lanes = set(metadata.get("planned_lanes") or [])
            planned_lanes.add(pool.lane_id)
            metadata["planned_lanes"] = sorted(planned_lanes)
            lane_grounding = dict(metadata.get("planned_lane_grounding") or {})
            grounding_score = max(
                float(lane_grounding.get(pool.lane_id) or 0.0),
                planned_lane_grounding(chunk, pool.lane_id),
                _lane_grounding_score(chunk, pool),
            )
            lane_grounding[pool.lane_id] = grounding_score
            metadata["planned_lane_grounding"] = lane_grounding
            lane_affinity = dict(metadata.get("planned_lane_affinity") or {})
            lane_affinity[pool.lane_id] = max(
                float(lane_affinity.get(pool.lane_id) or 0.0),
                grounding_score,
                planned_document_route_score(chunk, pool.lane_id),
            )
            metadata["planned_lane_affinity"] = lane_affinity
            metadata = merge_planned_attribution(metadata, {})
            metadata["planned_rrf_score"] = scores[key]
            representative.metadata = metadata
            provenance = list(previous_provenance)
            provenance.extend(
                item
                for item in (representative.provenance or [])
                if item not in provenance
            )
            marker = {
                "retriever": f"planned_{pool.retriever}",
                "lane_id": pool.lane_id,
                "rank": rank,
            }
            if marker not in provenance:
                provenance.append(marker)
            representative.provenance = provenance

    ordered_keys = sorted(
        representatives,
        key=lambda key: (
            -scores.get(key, 0.0),
            str(representatives[key].corpus_id or ""),
            str(representatives[key].doc_id or ""),
            key,
        ),
    )
    required_lanes = [pool.lane_id for pool in pools if pool.required]
    required_lanes = list(dict.fromkeys(required_lanes))
    selected: list[str] = []
    selected_set: set[str] = set()

    def reserve(key: str | None) -> None:
        if key and key not in selected_set and len(selected) < max_candidates:
            selected.append(key)
            selected_set.add(key)

    for lane_id in required_lanes:
        candidates = [
            key for key in lane_keys.get(lane_id, []) if key in representatives
        ]
        reserve(
            max(
                candidates,
                key=lambda key: (
                    planned_document_route_score(representatives[key], lane_id),
                    planned_lane_grounding(representatives[key], lane_id),
                    _has_lane_retriever(representatives[key], lane_id, "lexical"),
                    scores.get(key, 0.0),
                ),
                default=None,
            )
        )
    for corpus_id in corpus_ids or []:
        reserve(
            next(
                (
                    key
                    for key in ordered_keys
                    if str(representatives[key].corpus_id or "") == str(corpus_id)
                ),
                None,
            )
        )
    for key in ordered_keys:
        reserve(key)

    top_score = max((scores.get(key, 0.0) for key in selected), default=1.0) or 1.0
    output: list[SourceChunk] = []
    for key in selected:
        chunk = representatives[key]
        chunk.score = scores.get(key, 0.0) / top_score
        output.append(chunk)
    output.sort(
        key=lambda chunk: (
            -float(chunk.score or 0.0),
            str(chunk.corpus_id or ""),
            str(chunk.doc_id or ""),
            _candidate_key(chunk),
        )
    )
    return output, {
        "input_candidates": sum(len(pool.chunks) for pool in pools),
        "unique_candidates": len(representatives),
        "selected_candidates": len(output),
        "required_lanes": required_lanes,
        "retriever_counts": retriever_counts,
        "excluded_operational_artifacts": excluded_operational_artifacts,
    }


def reserve_planned_finalists(
    ranked: list[SourceChunk],
    preferred: list[SourceChunk],
    *,
    required_lane_ids: list[str],
    corpus_ids: list[str] | None,
    max_candidates: int,
    max_per_document: int | None = None,
    routed_document_budget: int = 0,
    preferred_route_lane_ids: list[str] | None = None,
    protected_lane_ids: list[str] | None = None,
    lane_seat_quotas: dict[str, int] | None = None,
) -> tuple[list[SourceChunk], dict[str, object]]:
    """Keep required semantic sides and corpora through the final rank cut.

    Fusion reservations protect recall before reranking, but a cross-encoder can
    otherwise remove every candidate for one side of a comparative query. This
    selector starts with normal diversity selection and adds/replaces only a
    missing required core lane or selected corpus. It never changes reranker
    scores or invokes a second scoring pass.
    """

    limit = max(1, int(max_candidates))
    by_key = {_candidate_key(chunk): chunk for chunk in ranked if _candidate_key(chunk)}
    ranked_keys = list(by_key)
    top_score = max((float(chunk.score or 0.0) for chunk in ranked), default=0.0)

    def reservation_relevant(chunk: SourceChunk, lane_id: str) -> bool:
        if not planned_lane_supported(chunk, lane_id):
            return False
        planned_lanes = set((chunk.metadata or {}).get("planned_lanes") or [])
        if (
            lane_id in planned_lanes
            and planned_document_route_score(chunk, lane_id)
            >= DOCUMENT_ROUTE_GROUNDING_THRESHOLD
        ):
            # Document routing is a scope gate, not a score bonus. Once the
            # lane retrieved a descendant from a semantically accepted route,
            # a sharply calibrated reranker may order candidates within that
            # lane but cannot erase the lane from the final packet.
            return True
        score = float(chunk.score or 0.0)
        if 0.0 <= score <= top_score <= 1.0 and top_score > 0.0:
            return score >= max(0.05, top_score * LANE_RESERVATION_MIN_SCORE_RATIO)
        return True

    selected: list[str] = []
    selected_set: set[str] = set()
    protected_keys: set[str] = set()
    lane_reservations: dict[str, str] = {}
    lane_quota_reservations: dict[str, list[str]] = {}
    lane_ordered_candidates: dict[str, list[str]] = {}
    protected_lane_reservations: dict[str, str] = {}
    lane_candidate_diagnostics: dict[str, dict[str, object]] = {}
    corpus_reservations: dict[str, str] = {}
    skipped_corpus_reservations: list[str] = []
    routed_document_reservations: dict[str, str] = {}
    document_counts: dict[str, int] = {}

    def reserve(
        key: str | None,
        *,
        allow_overflow: bool = False,
        ignore_document_cap: bool = False,
    ) -> bool:
        if not key or key in selected_set:
            return False
        if len(selected) >= limit and not allow_overflow:
            return False
        document_key = _candidate_document_key(by_key[key])
        if (
            not ignore_document_cap
            and max_per_document is not None
            and document_key
            and document_counts.get(document_key, 0) >= max(1, max_per_document)
        ):
            return False
        selected.append(key)
        selected_set.add(key)
        if document_key:
            document_counts[document_key] = document_counts.get(document_key, 0) + 1
        return True

    def replace_unprotected_document_candidate(key: str | None) -> bool:
        """Swap a same-document diversity winner for required lane evidence."""

        if not key or key in selected_set:
            return bool(key and key in selected_set)
        document_key = _candidate_document_key(by_key[key])
        if not document_key:
            return False
        replace_key = next(
            (
                current
                for current in selected
                if current not in protected_keys
                and _candidate_document_key(by_key[current]) == document_key
            ),
            None,
        )
        if not replace_key:
            return False
        index = selected.index(replace_key)
        selected[index] = key
        selected_set.discard(replace_key)
        selected_set.add(key)
        return True

    # Diversity selection is the primary packet. Reservations add only a
    # missing semantic side/corpus; they must not repopulate every empty slot
    # with lower-quality ranked candidates.
    for chunk in preferred:
        reserve(_candidate_key(chunk))

    # The exact user query is a recall safety lane, not another semantic
    # obligation. Preserve its strongest cross-encoder result without counting
    # it toward required-concept coverage or granting it route authority.
    for lane_id in dict.fromkeys(protected_lane_ids or []):
        existing_key = next(
            (
                candidate_key
                for candidate_key in selected
                if lane_id
                in set(
                    (by_key[candidate_key].metadata or {}).get("planned_lanes") or []
                )
            ),
            None,
        )
        key = existing_key or next(
            (
                candidate_key
                for candidate_key in ranked_keys
                if lane_id
                in set(
                    (by_key[candidate_key].metadata or {}).get("planned_lanes") or []
                )
            ),
            None,
        )
        if key and (
            key in selected_set
            or reserve(
                key,
                allow_overflow=True,
                ignore_document_cap=True,
            )
        ):
            protected_keys.add(key)
            protected_lane_reservations[lane_id] = key

    for lane_id, key in protected_lane_reservations.items():
        metadata = dict(by_key[key].metadata or {})
        reservations = set(metadata.get("planned_protected_lane_reservations") or [])
        reservations.add(lane_id)
        metadata["planned_protected_lane_reservations"] = sorted(reservations)
        by_key[key].metadata = metadata

    for lane_id in dict.fromkeys(required_lane_ids):
        existing_key = next(
            (
                candidate_key
                for candidate_key in selected
                if planned_lane_supported(by_key[candidate_key], lane_id)
            ),
            None,
        )
        supported_candidates = [
            candidate_key
            for candidate_key in ranked_keys
            if lane_id
            in set((by_key[candidate_key].metadata or {}).get("planned_lanes") or [])
            and planned_lane_supported(by_key[candidate_key], lane_id)
        ]
        candidates = [
            candidate_key
            for candidate_key in supported_candidates
            if reservation_relevant(by_key[candidate_key], lane_id)
        ]
        best_supported = max(
            supported_candidates,
            key=lambda candidate_key: (
                planned_document_route_score(by_key[candidate_key], lane_id),
                planned_lane_grounding(by_key[candidate_key], lane_id),
                float(by_key[candidate_key].score or 0.0),
                -ranked_keys.index(candidate_key),
            ),
            default=None,
        )
        best_chunk = by_key.get(best_supported) if best_supported else None
        best_score = float(best_chunk.score or 0.0) if best_chunk else 0.0
        max_supported_score = max(
            (float(by_key[key].score or 0.0) for key in supported_candidates),
            default=0.0,
        )
        lane_candidate_diagnostics[lane_id] = {
            "supported_candidates": len(supported_candidates),
            "score_eligible_candidates": len(candidates),
            "best_supported_score": round(best_score, 4),
            "best_supported_score_ratio": round(best_score / top_score, 4)
            if top_score > 0.0
            else 0.0,
            "max_supported_score": round(max_supported_score, 4),
            "max_supported_score_ratio": round(max_supported_score / top_score, 4)
            if top_score > 0.0
            else 0.0,
            "best_document_route_score": round(
                planned_document_route_score(best_chunk, lane_id), 4
            )
            if best_chunk
            else 0.0,
            "best_literal_grounding": round(
                planned_lane_grounding(best_chunk, lane_id), 4
            )
            if best_chunk
            else 0.0,
        }
        ordered_candidates = sorted(
            candidates,
            key=lambda candidate_key: (
                planned_lane_grounding(by_key[candidate_key], lane_id)
                >= LANE_GROUNDING_THRESHOLD,
                float(by_key[candidate_key].score or 0.0),
                planned_document_route_score(by_key[candidate_key], lane_id),
                planned_lane_grounding(by_key[candidate_key], lane_id),
                _has_lane_retriever(by_key[candidate_key], lane_id, "lexical"),
                -ranked_keys.index(candidate_key),
            ),
            reverse=True,
        )
        lane_ordered_candidates[lane_id] = ordered_candidates
        # A previously selected route-only candidate may technically support
        # the lane while the reranker has scored it near zero. Do not let that
        # first-selected candidate short-circuit a much stronger grounded
        # passage for the same obligation.
        key = ordered_candidates[0] if ordered_candidates else existing_key
        if key not in selected_set:
            for candidate_key in ordered_candidates:
                if candidate_key != key:
                    continue
                if reserve(candidate_key, allow_overflow=True) or (
                    max_per_document is not None
                    and replace_unprotected_document_candidate(candidate_key)
                ):
                    key = candidate_key
                    break
        if key is None or key not in selected_set:
            # A hard document-diversity cap must not make a required semantic
            # side disappear when its only grounded passage is a different
            # chunk in a document already reserved for another side.
            for candidate_key in ordered_candidates:
                if reserve(
                    candidate_key,
                    allow_overflow=True,
                    ignore_document_cap=True,
                ):
                    key = candidate_key
                    break
        if key is not None:
            protected_keys.add(key)
            lane_reservations[lane_id] = key

    allocated_quota_keys: set[str] = set()
    protected_outside_required_lanes = {
        key
        for key in set(protected_lane_reservations.values())
        if not any(
            planned_lane_supported(by_key[key], lane_id)
            for lane_id in dict.fromkeys(required_lane_ids)
        )
    }
    requested_quotas, effective_quotas = _bounded_lane_seat_quotas(
        required_lane_ids,
        lane_seat_quotas,
        limit=max(
            0,
            limit - len(protected_outside_required_lanes),
        ),
    )
    for lane_id in dict.fromkeys(required_lane_ids):
        quota = effective_quotas[lane_id]
        ordered_candidates = lane_ordered_candidates.get(lane_id, [])
        reserved: list[str] = []
        first = lane_reservations.get(lane_id)
        if first and first not in allocated_quota_keys:
            reserved.append(first)
            allocated_quota_keys.add(first)
        if quota > len(reserved):
            for candidate_key in ordered_candidates:
                if len(reserved) >= quota:
                    break
                if candidate_key in allocated_quota_keys or candidate_key in reserved:
                    continue
                if candidate_key in selected_set or reserve(
                    candidate_key,
                    allow_overflow=True,
                ):
                    reserved.append(candidate_key)
                    allocated_quota_keys.add(candidate_key)
                    protected_keys.add(candidate_key)
        if reserved:
            lane_quota_reservations[lane_id] = reserved
            lane_reservations[lane_id] = reserved[0]
        else:
            lane_reservations.pop(lane_id, None)

    for lane_id, keys in lane_quota_reservations.items():
        for key in keys:
            metadata = dict(by_key[key].metadata or {})
            reservations = set(metadata.get("planned_required_lane_reservations") or [])
            reservations.add(lane_id)
            metadata["planned_required_lane_reservations"] = sorted(reservations)
            by_key[key].metadata = metadata

    # Routing is a scope prior, not a score bonus. Preserve the strongest
    # independently relevant candidate from a bounded number of routed
    # documents before wildcard/global candidates consume the final packet.
    route_candidates: dict[str, tuple[float, int, str]] = {}
    preferred_route_lanes = set(preferred_route_lane_ids or [])
    for rank, candidate_key in enumerate(ranked_keys):
        chunk = by_key[candidate_key]
        route_scores = dict((chunk.metadata or {}).get("document_route_lanes") or {})
        if preferred_route_lanes:
            route_scores = {
                lane_id: value
                for lane_id, value in route_scores.items()
                if lane_id in preferred_route_lanes
            }
        if not route_scores:
            continue
        document_key = _candidate_document_key(chunk)
        if not document_key:
            continue
        route_score = max(
            (float(value or 0.0) for value in route_scores.values()), default=0.0
        )
        if route_score <= 0.0 or not any(
            reservation_relevant(chunk, lane_id) for lane_id in route_scores
        ):
            continue
        candidate = (route_score, -rank, candidate_key)
        if candidate > route_candidates.get(document_key, (-1.0, -(10**9), "")):
            route_candidates[document_key] = candidate

    route_limit = min(
        max(0, int(routed_document_budget)),
        max(0, limit - 1),
        len(route_candidates),
    )
    routed_docs_already_selected = {
        _candidate_document_key(by_key[key])
        for key in selected
        if dict((by_key[key].metadata or {}).get("document_route_lanes") or {})
    }
    for document_key, (_route_score, _rank, key) in sorted(
        route_candidates.items(),
        key=lambda item: (-item[1][0], -item[1][1], item[0]),
    ):
        if len(routed_docs_already_selected) >= route_limit:
            break
        if document_key in routed_docs_already_selected:
            continue
        if reserve(key, allow_overflow=True):
            protected_keys.add(key)
            routed_docs_already_selected.add(document_key)
            routed_document_reservations[document_key] = key

    def corpus_reservation_relevant(chunk: SourceChunk) -> bool:
        """A selected corpus gets a seat only when it has relevant evidence.

        Unbounded/logit score families are not ratio-comparable; the shared
        gate preserves the prior behavior for them until that score family
        has an explicit calibration.
        """

        return passes_corpus_reservation(float(chunk.score or 0.0), top_score)

    corpus_reservation_details: dict[str, dict[str, object]] = {}
    for corpus_id in corpus_ids or []:
        cid = str(corpus_id)
        existing_key = next(
            (
                candidate_key
                for candidate_key in selected
                if str(by_key[candidate_key].corpus_id or "") == cid
            ),
            None,
        )
        # An already-selected candidate earns corpus PROTECTION only through
        # the same calibrated gate as a fresh reservation (P0.3): protection
        # shields it from the overflow trim, so a sub-bound candidate must
        # not hold the corpus seat. It keeps its diversity seat either way.
        key = None
        if existing_key is not None and corpus_reservation_relevant(
            by_key[existing_key]
        ):
            key = existing_key
        if key is None:
            key = next(
                (
                    candidate_key
                    for candidate_key in ranked_keys
                    if str(by_key[candidate_key].corpus_id or "") == cid
                    and corpus_reservation_relevant(by_key[candidate_key])
                ),
                None,
            )
        corpus_scores = [
            float(by_key[candidate_key].score or 0.0)
            for candidate_key in ranked_keys
            if str(by_key[candidate_key].corpus_id or "") == cid
        ]
        detail: dict[str, object] = {
            "best_score": round(max(corpus_scores), 4) if corpus_scores else None,
            "bound": corpus_reservation_bound(top_score),
        }
        if key:
            reserve(key, allow_overflow=True)
            protected_keys.add(key)
            corpus_reservations[cid] = key
            detail["outcome"] = (
                "existing_selected" if key == existing_key else "reserved_ranked"
            )
        else:
            skipped_corpus_reservations.append(cid)
            detail["outcome"] = (
                "below_reservation_bound" if corpus_scores else "no_candidate"
            )
        corpus_reservation_details[cid] = detail

    for corpus_id, key in corpus_reservations.items():
        metadata = dict(by_key[key].metadata or {})
        reservations = set(metadata.get("planned_corpus_reservations") or [])
        reservations.add(corpus_id)
        metadata["planned_corpus_reservations"] = sorted(reservations)
        by_key[key].metadata = metadata

    # If adding required evidence exceeded the limit, remove the weakest
    # unprotected diversity winner. Required semantic/corpus evidence survives.
    ordered_selected = [key for key in ranked_keys if key in selected_set]
    quota_protected_keys = {
        key for keys in lane_quota_reservations.values() for key in keys
    }
    while len(ordered_selected) > limit:
        removable = next(
            (key for key in reversed(ordered_selected) if key not in protected_keys),
            None,
        )
        if removable is None and lane_seat_quotas:
            removable = next(
                (
                    key
                    for key in reversed(ordered_selected)
                    if key not in quota_protected_keys
                ),
                None,
            )
        if removable is None:
            break
        ordered_selected.remove(removable)
        selected_set.discard(removable)
        document_key = _candidate_document_key(by_key[removable])
        if document_key:
            document_counts[document_key] = max(
                0, document_counts.get(document_key, 0) - 1
            )

    # Preserve the cross-encoder's order among the selected candidates.
    output = [by_key[key] for key in ranked_keys if key in selected_set][:limit]
    routed_document_refs_selected = sorted(
        {
            _candidate_document_key(chunk)
            for chunk in output
            if dict((chunk.metadata or {}).get("document_route_lanes") or {})
            and _candidate_document_key(chunk)
        }
    )
    routed_documents_selected = sorted(
        {
            str(chunk.doc_id or "")
            for chunk in output
            if dict((chunk.metadata or {}).get("document_route_lanes") or {})
            and str(chunk.doc_id or "")
        }
    )

    def public_candidate_ids(values: dict[str, str]) -> dict[str, str]:
        return {
            label: str(by_key[key].chunk_id or by_key[key].parent_id or "")
            for label, key in values.items()
        }

    receipt = {
        "required_lane_ids": list(dict.fromkeys(required_lane_ids)),
        "protected_lane_ids": list(dict.fromkeys(protected_lane_ids or [])),
        "protected_lane_reservations": public_candidate_ids(
            protected_lane_reservations
        ),
        "protected_lane_reservation_refs": protected_lane_reservations,
        "lane_reservations": public_candidate_ids(lane_reservations),
        "lane_reservation_refs": lane_reservations,
        "lane_seat_quotas_requested": requested_quotas,
        "lane_seat_quotas": effective_quotas,
        "lane_quota_reservations": {
            lane_id: [
                str(by_key[key].chunk_id or by_key[key].parent_id or "") for key in keys
            ]
            for lane_id, keys in lane_quota_reservations.items()
        },
        "lane_quota_reservation_refs": lane_quota_reservations,
        "lane_quota_fulfilled": {
            lane_id: len(lane_quota_reservations.get(lane_id, []))
            for lane_id in requested_quotas
        },
        "lane_quota_spillover": {
            lane_id: max(
                0,
                requested_quotas[lane_id]
                - len(lane_quota_reservations.get(lane_id, [])),
            )
            for lane_id in requested_quotas
        },
        "lane_candidates": lane_candidate_diagnostics,
        "corpus_reservations": public_candidate_ids(corpus_reservations),
        "corpus_reservation_refs": corpus_reservations,
        "skipped_corpus_reservations": skipped_corpus_reservations,
        "corpus_reservation_details": corpus_reservation_details,
        "routed_document_reservations": {
            str(by_key[key].doc_id or document_ref): str(
                by_key[key].chunk_id or by_key[key].parent_id or ""
            )
            for document_ref, key in routed_document_reservations.items()
        },
        "routed_document_reservation_refs": routed_document_reservations,
        "routed_documents_selected": routed_documents_selected,
        "routed_document_refs_selected": routed_document_refs_selected,
        "routed_document_budget": route_limit,
        "preferred_route_lane_ids": sorted(preferred_route_lanes),
        "selected_candidates": len(output),
        "max_per_document": max_per_document,
    }
    return output, _reconcile_lane_quota_receipt(
        output,
        receipt,
        requested_quotas=requested_quotas,
        effective_quotas=effective_quotas,
    )


def seated_document_refs_by_lane(
    finalists: list[SourceChunk],
    required_lane_ids: list[str],
) -> dict[str, set[tuple[str, str]]]:
    """Return actual lane reservations, never broad candidate attribution."""

    required = set(required_lane_ids)
    output: dict[str, set[tuple[str, str]]] = {
        lane_id: set() for lane_id in required_lane_ids
    }
    for chunk in finalists:
        corpus_id = str(chunk.corpus_id or "")
        doc_id = str(chunk.doc_id or "")
        if not corpus_id or not doc_id:
            continue
        reservations = set(
            (chunk.metadata or {}).get("planned_required_lane_reservations") or []
        )
        for lane_id in reservations & required:
            output[lane_id].add((corpus_id, doc_id))
    return output


def prioritize_enumeration_candidates(
    ranked: list[SourceChunk],
    preferred: list[SourceChunk],
    *,
    answer_lane_ids: list[str],
    required_lane_ids: list[str],
    max_candidates: int,
) -> tuple[list[SourceChunk], dict[str, object]]:
    """Allocate enumeration context to answer objects before support prose."""

    answer_lanes = list(dict.fromkeys(answer_lane_ids))
    if not answer_lanes:
        return preferred[: max(1, int(max_candidates))], {"applied": False}

    limit = max(1, int(max_candidates))
    support_lanes = [
        lane_id
        for lane_id in dict.fromkeys(required_lane_ids)
        if lane_id not in answer_lanes
    ]
    answer_budget = max(2, limit - min(2, len(support_lanes)))
    top_score = max((float(chunk.score or 0.0) for chunk in ranked), default=0.0)
    selected: list[SourceChunk] = []
    selected_keys: set[str] = set()
    parent_counts: dict[str, int] = {}
    support_document_counts: dict[str, int] = {}

    def add(chunk: SourceChunk, *, answer: bool = False) -> bool:
        key = _candidate_key(chunk)
        if not key or key in selected_keys or len(selected) >= limit:
            return False
        parent_key = (
            f"{str(chunk.corpus_id or '')}|{str(chunk.parent_id or '').strip()}"
            if chunk.parent_id
            else ""
        )
        document_key = _candidate_document_key(chunk)
        if answer and parent_key and parent_counts.get(parent_key, 0) >= 2:
            return False
        if (
            not answer
            and document_key
            and support_document_counts.get(document_key, 0) >= 1
        ):
            return False
        selected.append(chunk)
        selected_keys.add(key)
        if answer and parent_key:
            parent_counts[parent_key] = parent_counts.get(parent_key, 0) + 1
        elif document_key:
            support_document_counts[document_key] = (
                support_document_counts.get(document_key, 0) + 1
            )
        return True

    answer_count = 0
    for chunk in ranked:
        if answer_count >= answer_budget:
            break
        if not any(planned_lane_supported(chunk, lane_id) for lane_id in answer_lanes):
            continue
        score = float(chunk.score or 0.0)
        if 0.0 < top_score <= 1.0 and score < top_score * 0.20:
            continue
        if add(chunk, answer=True):
            answer_count += 1

    support_count = 0
    for lane_id in support_lanes:
        support = next(
            (
                chunk
                for chunk in [*preferred, *ranked]
                if planned_lane_supported(chunk, lane_id)
                and _candidate_key(chunk) not in selected_keys
            ),
            None,
        )
        if support is not None and add(support):
            support_count += 1

    for chunk in preferred:
        add(chunk)

    return selected, {
        "applied": True,
        "answer_lane_ids": answer_lanes,
        "support_lane_ids": support_lanes,
        "answer_budget": answer_budget,
        "answer_candidates": answer_count,
        "support_candidates": support_count,
        "selected_candidates": len(selected),
    }


def dedupe_enumeration_finalists(
    chunks: list[SourceChunk],
    *,
    answer_lane_ids: list[str],
    max_answer_per_parent: int = 2,
) -> tuple[list[SourceChunk], int]:
    """Preserve answer siblings while capping repeated support-document prose."""

    answer_lanes = set(answer_lane_ids)
    output: list[SourceChunk] = []
    seen_keys: set[str] = set()
    answer_parent_counts: dict[str, int] = {}
    support_document_counts: dict[str, int] = {}
    dropped = 0
    for chunk in chunks:
        key = _candidate_key(chunk)
        if key and key in seen_keys:
            dropped += 1
            continue
        is_answer = any(
            planned_lane_supported(chunk, lane_id) for lane_id in answer_lanes
        )
        parent_key = str(chunk.parent_id or "").strip()
        document_key = _candidate_document_key(chunk)
        if is_answer:
            if parent_key and answer_parent_counts.get(parent_key, 0) >= max(
                1, int(max_answer_per_parent)
            ):
                dropped += 1
                continue
            if parent_key:
                answer_parent_counts[parent_key] = (
                    answer_parent_counts.get(parent_key, 0) + 1
                )
        elif document_key:
            if support_document_counts.get(document_key, 0) >= 1:
                dropped += 1
                continue
            support_document_counts[document_key] = 1
        if key:
            seen_keys.add(key)
        output.append(chunk)
    return output, dropped


def order_enumeration_finalists(
    chunks: list[SourceChunk], *, answer_lane_ids: list[str]
) -> list[SourceChunk]:
    """Present requested answer objects before bounded supporting context."""

    answer_lanes = set(answer_lane_ids)
    return [
        chunk
        for _index, chunk in sorted(
            enumerate(chunks),
            key=lambda item: (
                not any(
                    planned_lane_supported(item[1], lane_id) for lane_id in answer_lanes
                ),
                item[0],
            ),
        )
    ]


def dedupe_parent_finalists(
    chunks: list[SourceChunk],
) -> tuple[list[SourceChunk], int]:
    """Collapse multiple child winners that hydrate to the same parent."""

    output: list[SourceChunk] = []
    index_by_parent: dict[tuple[str, str], int] = {}
    dropped = 0
    for chunk in chunks:
        parent_id = str(chunk.parent_id or "").strip()
        if not parent_id:
            output.append(chunk)
            continue
        parent_key = (str(chunk.corpus_id or ""), parent_id)
        existing_index = index_by_parent.get(parent_key)
        if existing_index is None:
            index_by_parent[parent_key] = len(output)
            output.append(chunk.model_copy(deep=True))
            continue
        dropped += 1
        existing = output[existing_index]
        existing_meta = dict(existing.metadata or {})
        duplicate_meta = dict(chunk.metadata or {})
        for list_key in (
            "planned_lanes",
            "planned_required_lane_reservations",
            "corpus_memberships",
        ):
            merged = {
                str(value)
                for value in (
                    list(existing_meta.get(list_key) or [])
                    + list(duplicate_meta.get(list_key) or [])
                )
                if value
            }
            if list_key == "corpus_memberships":
                merged.update(
                    str(value)
                    for value in (existing.corpus_id, chunk.corpus_id)
                    if value
                )
            if merged:
                existing_meta[list_key] = sorted(merged)
        for score_map_key in (
            "planned_lane_grounding",
            "document_route_lanes",
        ):
            merged_scores = dict(existing_meta.get(score_map_key) or {})
            for lane_id, value in dict(duplicate_meta.get(score_map_key) or {}).items():
                try:
                    merged_scores[str(lane_id)] = max(
                        float(merged_scores.get(str(lane_id)) or 0.0),
                        float(value or 0.0),
                    )
                except (TypeError, ValueError):
                    continue
            if merged_scores:
                existing_meta[score_map_key] = merged_scores
        existing.metadata = existing_meta
        provenance = list(existing.provenance or [])
        provenance.extend(
            item for item in (chunk.provenance or []) if item not in provenance
        )
        provenance.append(
            {
                "retriever": "parent_finalist_dedupe",
                "chunk_id": chunk.chunk_id,
                "parent_id": parent_id,
            }
        )
        existing.provenance = provenance
    return output, dropped


def dedupe_document_lane_finalists(
    chunks: list[SourceChunk],
) -> tuple[list[SourceChunk], int]:
    """Collapse repeated evidence from one document for the same plan lanes."""

    output: list[SourceChunk] = []
    seen: dict[tuple[str, tuple[str, ...]], SourceChunk] = {}
    dropped = 0
    for chunk in chunks:
        grounding = dict((chunk.metadata or {}).get("planned_lane_grounding") or {})
        lane_signature = tuple(
            sorted(
                str(lane_id)
                for lane_id, value in grounding.items()
                if float(value or 0.0) >= LANE_GROUNDING_THRESHOLD
            )
        )
        doc_id = _candidate_document_key(chunk)
        if not doc_id or not lane_signature:
            output.append(chunk)
            continue
        key = (doc_id, lane_signature)
        existing = seen.get(key)
        if existing is None:
            seen[key] = chunk
            output.append(chunk)
            continue

        dropped += 1
        existing_meta = dict(existing.metadata or {})
        duplicate_meta = dict(chunk.metadata or {})
        for list_key in (
            "corpus_memberships",
            "planned_retrievers",
            "planned_lanes",
            "planned_required_lane_reservations",
        ):
            merged = set(existing_meta.get(list_key) or [])
            merged.update(duplicate_meta.get(list_key) or [])
            if list_key == "corpus_memberships":
                merged.update(
                    str(value)
                    for value in (existing.corpus_id, chunk.corpus_id)
                    if value
                )
            if merged:
                existing_meta[list_key] = sorted(str(value) for value in merged)
        for score_map_key in (
            "planned_lane_grounding",
            "document_route_lanes",
        ):
            merged_scores = dict(existing_meta.get(score_map_key) or {})
            for lane_id, value in dict(duplicate_meta.get(score_map_key) or {}).items():
                try:
                    merged_scores[str(lane_id)] = max(
                        float(merged_scores.get(str(lane_id)) or 0.0),
                        float(value or 0.0),
                    )
                except (TypeError, ValueError):
                    continue
            if merged_scores:
                existing_meta[score_map_key] = merged_scores
        existing.metadata = existing_meta
        provenance = list(existing.provenance or [])
        provenance.extend(
            item for item in (chunk.provenance or []) if item not in provenance
        )
        existing.provenance = provenance
    return output, dropped

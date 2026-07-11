"""Rank-only fusion for QueryPlanV2 candidate lanes."""

from __future__ import annotations

import re
from dataclasses import dataclass

from models.schemas import SourceChunk


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


def grounded_planned_lane_ids(
    chunks: list[SourceChunk],
    required_lane_ids: list[str],
) -> list[str]:
    """Return required lanes backed by grounded evidence, not provenance alone."""
    return sorted(
        lane_id
        for lane_id in dict.fromkeys(required_lane_ids)
        if any(
            planned_lane_grounding(chunk, lane_id) >= LANE_GROUNDING_THRESHOLD
            for chunk in chunks
        )
    )


def filter_grounded_planned_candidates(
    chunks: list[SourceChunk],
    required_lane_ids: list[str],
) -> tuple[list[SourceChunk], dict[str, object]]:
    """Remove generic quota fillers from a fully grounded multi-side pack.

    The filter is intentionally conditional. It activates only when at least
    two semantic sides are required and grounded evidence for every side is
    already present. Graph relation/bridge candidates remain eligible when
    their text or structured provenance grounds at least one required side.
    """
    required = list(dict.fromkeys(required_lane_ids))
    diagnostics: dict[str, object] = {
        "enabled": bool(required),
        "input": len(chunks),
        "kept": len(chunks),
        "dropped": 0,
        "required_lane_ids": required,
        "grounded_lane_ids": grounded_planned_lane_ids(chunks, required),
        "applied": False,
    }
    if not required:
        diagnostics["reason"] = "no_required_lanes"
        return chunks, diagnostics
    grounded_lane_ids = list(diagnostics["grounded_lane_ids"])
    if not grounded_lane_ids:
        diagnostics["reason"] = "no_grounded_coverage"
        return chunks, diagnostics

    filtered = [
        chunk
        for chunk in chunks
        if any(
            planned_lane_grounding(chunk, lane_id) >= LANE_GROUNDING_THRESHOLD
            for lane_id in grounded_lane_ids
        )
    ]
    if not filtered:
        diagnostics["reason"] = "empty_filter_guard"
        return chunks, diagnostics

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
        }
    )
    return filtered, diagnostics


def _candidate_key(chunk: SourceChunk) -> str:
    return str(chunk.chunk_id or chunk.parent_id or "")


def _has_lane_retriever(chunk: SourceChunk, lane_id: str, retriever: str) -> bool:
    expected = f"planned_{retriever}"
    return any(
        isinstance(item, dict)
        and str(item.get("lane_id") or "") == lane_id
        and str(item.get("retriever") or "") == expected
        for item in (chunk.provenance or [])
    )


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
    for pool in pools:
        weight = retriever_weights.get(pool.retriever, 0.8)
        retriever_counts[pool.retriever] = retriever_counts.get(
            pool.retriever, 0
        ) + len(pool.chunks)
        lane_bucket = lane_keys.setdefault(pool.lane_id, [])
        for rank, chunk in enumerate(pool.chunks):
            key = _candidate_key(chunk)
            if not key:
                continue
            if key not in lane_bucket:
                lane_bucket.append(key)
            scores[key] = scores.get(key, 0.0) + weight / (rrf_k + rank + 1.0)
            existing = representatives.get(key)
            if existing is None or float(chunk.score or 0.0) > float(
                existing.score or 0.0
            ):
                representatives[key] = chunk.model_copy(deep=True)
            representative = representatives[key]
            metadata = dict(representative.metadata or {})
            planned_lanes = set(metadata.get("planned_lanes") or [])
            planned_lanes.add(pool.lane_id)
            metadata["planned_lanes"] = sorted(planned_lanes)
            lane_grounding = dict(metadata.get("planned_lane_grounding") or {})
            lane_grounding[pool.lane_id] = max(
                float(lane_grounding.get(pool.lane_id) or 0.0),
                _lane_grounding_score(chunk, pool),
            )
            metadata["planned_lane_grounding"] = lane_grounding
            metadata["planned_rrf_score"] = scores[key]
            representative.metadata = metadata
            provenance = list(representative.provenance or [])
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
    }


def reserve_planned_finalists(
    ranked: list[SourceChunk],
    preferred: list[SourceChunk],
    *,
    required_lane_ids: list[str],
    corpus_ids: list[str] | None,
    max_candidates: int,
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
    selected: list[str] = []
    selected_set: set[str] = set()
    protected_keys: set[str] = set()
    lane_reservations: dict[str, str] = {}
    corpus_reservations: dict[str, str] = {}

    def reserve(key: str | None, *, allow_overflow: bool = False) -> bool:
        if not key or key in selected_set:
            return False
        if len(selected) >= limit and not allow_overflow:
            return False
        selected.append(key)
        selected_set.add(key)
        return True

    # Diversity selection is the primary packet. Reservations add only a
    # missing semantic side/corpus; they must not repopulate every empty slot
    # with lower-quality ranked candidates.
    for chunk in preferred:
        reserve(_candidate_key(chunk))

    for lane_id in dict.fromkeys(required_lane_ids):
        existing_key = next(
            (
                candidate_key
                for candidate_key in selected
                if planned_lane_grounding(by_key[candidate_key], lane_id)
                >= LANE_GROUNDING_THRESHOLD
            ),
            None,
        )
        candidates = [
            candidate_key
            for candidate_key in ranked_keys
            if lane_id
            in set((by_key[candidate_key].metadata or {}).get("planned_lanes") or [])
        ]
        key = existing_key or max(
            candidates,
            key=lambda candidate_key: (
                planned_lane_grounding(by_key[candidate_key], lane_id),
                _has_lane_retriever(by_key[candidate_key], lane_id, "lexical"),
                -ranked_keys.index(candidate_key),
            ),
            default=None,
        )
        if key:
            reserve(key, allow_overflow=True)
            protected_keys.add(key)
            lane_reservations[lane_id] = key

    for corpus_id in corpus_ids or []:
        existing_key = next(
            (
                candidate_key
                for candidate_key in selected
                if str(by_key[candidate_key].corpus_id or "") == str(corpus_id)
            ),
            None,
        )
        key = existing_key or next(
            (
                candidate_key
                for candidate_key in ranked_keys
                if str(by_key[candidate_key].corpus_id or "") == str(corpus_id)
            ),
            None,
        )
        if key:
            reserve(key, allow_overflow=True)
            protected_keys.add(key)
            corpus_reservations[str(corpus_id)] = key

    # If adding required evidence exceeded the limit, remove the weakest
    # unprotected diversity winner. Required semantic/corpus evidence survives.
    ordered_selected = [key for key in ranked_keys if key in selected_set]
    while len(ordered_selected) > limit:
        removable = next(
            (key for key in reversed(ordered_selected) if key not in protected_keys),
            None,
        )
        if removable is None:
            break
        ordered_selected.remove(removable)
        selected_set.discard(removable)

    # Preserve the cross-encoder's order among the selected candidates.
    output = [by_key[key] for key in ranked_keys if key in selected_set][:limit]
    return output, {
        "required_lane_ids": list(dict.fromkeys(required_lane_ids)),
        "lane_reservations": lane_reservations,
        "corpus_reservations": corpus_reservations,
        "selected_candidates": len(output),
    }


def dedupe_parent_finalists(
    chunks: list[SourceChunk],
) -> tuple[list[SourceChunk], int]:
    """Collapse multiple child winners that hydrate to the same parent."""

    output: list[SourceChunk] = []
    index_by_parent: dict[str, int] = {}
    dropped = 0
    for chunk in chunks:
        parent_key = str(chunk.parent_id or "").strip()
        if not parent_key:
            output.append(chunk)
            continue
        existing_index = index_by_parent.get(parent_key)
        if existing_index is None:
            index_by_parent[parent_key] = len(output)
            output.append(chunk.model_copy(deep=True))
            continue
        dropped += 1
        existing = output[existing_index]
        existing_meta = dict(existing.metadata or {})
        duplicate_meta = dict(chunk.metadata or {})
        for list_key in ("planned_lanes", "corpus_memberships"):
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
        merged_grounding = dict(existing_meta.get("planned_lane_grounding") or {})
        for lane_id, value in dict(
            duplicate_meta.get("planned_lane_grounding") or {}
        ).items():
            try:
                merged_grounding[str(lane_id)] = max(
                    float(merged_grounding.get(str(lane_id)) or 0.0),
                    float(value or 0.0),
                )
            except (TypeError, ValueError):
                continue
        if merged_grounding:
            existing_meta["planned_lane_grounding"] = merged_grounding
        existing.metadata = existing_meta
        provenance = list(existing.provenance or [])
        provenance.extend(
            item for item in (chunk.provenance or []) if item not in provenance
        )
        provenance.append(
            {
                "retriever": "parent_finalist_dedupe",
                "chunk_id": chunk.chunk_id,
                "parent_id": parent_key,
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
        doc_id = str(chunk.doc_id or "").strip()
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
        for list_key in ("corpus_memberships", "planned_retrievers", "planned_lanes"):
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
        existing.metadata = existing_meta
        provenance = list(existing.provenance or [])
        provenance.extend(
            item for item in (chunk.provenance or []) if item not in provenance
        )
        existing.provenance = provenance
    return output, dropped

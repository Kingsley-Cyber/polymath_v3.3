"""Rank-only fusion for QueryPlanV2 candidate lanes."""

from __future__ import annotations

from dataclasses import dataclass

from models.schemas import SourceChunk


@dataclass(frozen=True)
class PlannedPool:
    lane_id: str
    retriever: str
    chunks: tuple[SourceChunk, ...]
    required: bool = False


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
        retriever_counts[pool.retriever] = retriever_counts.get(pool.retriever, 0) + len(
            pool.chunks
        )
        lane_bucket = lane_keys.setdefault(pool.lane_id, [])
        for rank, chunk in enumerate(pool.chunks):
            key = _candidate_key(chunk)
            if not key:
                continue
            if key not in lane_bucket:
                lane_bucket.append(key)
            scores[key] = scores.get(key, 0.0) + weight / (rrf_k + rank + 1.0)
            existing = representatives.get(key)
            if existing is None or float(chunk.score or 0.0) > float(existing.score or 0.0):
                representatives[key] = chunk.model_copy(deep=True)
            representative = representatives[key]
            metadata = dict(representative.metadata or {})
            planned_lanes = set(metadata.get("planned_lanes") or [])
            planned_lanes.add(pool.lane_id)
            metadata["planned_lanes"] = sorted(planned_lanes)
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
    selector reserves the best reranked candidate for each required core lane,
    then one per selected corpus when space remains, and fills the rest from the
    normal diversity selection. It never changes reranker scores or invokes a
    second scoring pass.
    """

    limit = max(1, int(max_candidates))
    by_key = {_candidate_key(chunk): chunk for chunk in ranked if _candidate_key(chunk)}
    ranked_keys = list(by_key)
    selected: list[str] = []
    selected_set: set[str] = set()
    lane_reservations: dict[str, str] = {}
    corpus_reservations: dict[str, str] = {}

    def reserve(key: str | None) -> bool:
        if not key or key in selected_set or len(selected) >= limit:
            return False
        selected.append(key)
        selected_set.add(key)
        return True

    for lane_id in dict.fromkeys(required_lane_ids):
        candidates = [
            candidate_key
            for candidate_key in ranked_keys
            if lane_id
            in set((by_key[candidate_key].metadata or {}).get("planned_lanes") or [])
        ]
        key = max(
            candidates,
            key=lambda candidate_key: (
                _has_lane_retriever(by_key[candidate_key], lane_id, "lexical"),
                -ranked_keys.index(candidate_key),
            ),
            default=None,
        )
        if key:
            reserve(key)
            lane_reservations[lane_id] = key

    for corpus_id in corpus_ids or []:
        key = next(
            (
                candidate_key
                for candidate_key in ranked_keys
                if str(by_key[candidate_key].corpus_id or "") == str(corpus_id)
            ),
            None,
        )
        if key:
            reserve(key)
            corpus_reservations[str(corpus_id)] = key

    for chunk in preferred:
        reserve(_candidate_key(chunk))
    for key in ranked_keys:
        reserve(key)

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
        existing.metadata = existing_meta
        provenance = list(existing.provenance or [])
        provenance.extend(item for item in (chunk.provenance or []) if item not in provenance)
        provenance.append(
            {
                "retriever": "parent_finalist_dedupe",
                "chunk_id": chunk.chunk_id,
                "parent_id": parent_key,
            }
        )
        existing.provenance = provenance
    return output, dropped

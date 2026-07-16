import logging
from json import dumps
from typing import List

from models.schemas import SourceChunk

logger = logging.getLogger(__name__)


def _merge_source_tiers(*tiers: str | None) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for tier in tiers:
        for part in str(tier or "").split("+"):
            cleaned = part.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            parts.append(cleaned)
    return "+".join(parts) if parts else "unknown"


def _merge_provenance(*groups: list[dict] | None) -> list[dict] | None:
    merged: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        for item in group or []:
            if not isinstance(item, dict):
                continue
            key = dumps(item, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            merged.append(dict(item))
    return merged or None


def _is_summary_candidate(chunk: SourceChunk) -> bool:
    chunk_id = str(chunk.chunk_id or "").lower()
    tier = str(chunk.source_tier or "").lower()
    return chunk_id.endswith("_summary") or "summary" in tier


def _representative_priority(chunk: SourceChunk) -> int:
    """Prefer concrete child/evidence chunks over parent summaries.

    The parent keeps the strongest fused score, but reranking should read the
    most exact evidence text available for that parent whenever one exists.
    """

    return 0 if _is_summary_candidate(chunk) else 1


def _candidate_snapshot(chunk: SourceChunk) -> dict:
    return {
        "corpus_id": chunk.corpus_id,
        "chunk_id": chunk.chunk_id,
        "parent_id": chunk.parent_id,
        "doc_id": chunk.doc_id,
        "score": float(chunk.score or 0.0),
        "source_tier": chunk.source_tier,
        "is_summary": _is_summary_candidate(chunk),
    }


def _merge_representative_metadata(target: SourceChunk, *chunks: SourceChunk) -> None:
    metadata = dict(target.metadata or {})
    reps = list(metadata.get("merged_parent_representatives") or [])
    seen = {
        (
            str(item.get("corpus_id") or ""),
            str(item.get("chunk_id") or item.get("parent_id") or ""),
        )
        for item in reps
        if isinstance(item, dict)
    }
    for chunk in chunks:
        key = (
            str(chunk.corpus_id or ""),
            str(chunk.chunk_id or chunk.parent_id or ""),
        )
        if not key[1] or key in seen:
            continue
        seen.add(key)
        reps.append(_candidate_snapshot(chunk))
    reps.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    metadata["merged_parent_representatives"] = reps[:5]
    target.metadata = metadata


def merge_pools(
    *pools: List[SourceChunk],
    dedupe_by_parent: bool = True,
) -> List[SourceChunk]:
    """
    Merges multiple candidate pools of SourceChunk objects into a single, deduplicated pool.

    Deduplication and Coalescing Rules:
    - Child hits: Deduplicate by chunk_id natively, but group by parent_id for HRAG.
    - Summary hits: Deduplicate by parent_id when `dedupe_by_parent=True`.
    - Coalescence: If a summary and one or more child hits share the same parent_id,
      they are coalesced into a single candidate representing that parent.
    - The highest vector similarity score among the coalesced chunks is retained.
    """
    merged_dict = {}

    for pool in pools:
        if not pool:
            continue

        for chunk in pool:
            # Determine the grouping key. Hydrated tiers group by parent_id to
            # avoid hydrating the same parent text multiple times. Fast Search
            # is a pure Qdrant route, so callers may preserve parent summaries
            # and child vectors as distinct evidence by passing
            # dedupe_by_parent=False.
            key = (
                str(chunk.corpus_id or ""),
                str(
                    chunk.parent_id
                    if dedupe_by_parent and chunk.parent_id
                    else chunk.chunk_id
                ),
            )

            if key not in merged_dict:
                # We do a shallow copy to avoid mutating the original chunk in case it's cached
                merged_dict[key] = chunk.model_copy()
            else:
                existing_chunk = merged_dict[key]
                merged_source_tier = _merge_source_tiers(
                    existing_chunk.source_tier,
                    chunk.source_tier,
                )
                merged_provenance = _merge_provenance(
                    existing_chunk.provenance,
                    chunk.provenance,
                )

                keep_new_identity = _representative_priority(
                    chunk
                ) > _representative_priority(existing_chunk) or (
                    _representative_priority(chunk)
                    == _representative_priority(existing_chunk)
                    and chunk.score > existing_chunk.score
                )
                best_score = max(
                    float(existing_chunk.score or 0.0), float(chunk.score or 0.0)
                )
                if keep_new_identity:
                    # Inherit the stronger evidence identity/text, but keep the
                    # highest parent-level score and provenance from every lane.
                    old_chunk = existing_chunk
                    replacement = chunk.model_copy()
                    replacement.score = best_score
                    replacement.source_tier = merged_source_tier
                    replacement.provenance = merged_provenance
                    if old_chunk.summary and not replacement.summary:
                        replacement.summary = old_chunk.summary
                    _merge_representative_metadata(replacement, old_chunk, chunk)
                    merged_dict[key] = replacement
                    existing_chunk = replacement
                else:
                    existing_chunk.score = best_score
                    existing_chunk.source_tier = merged_source_tier
                    existing_chunk.provenance = merged_provenance
                    _merge_representative_metadata(
                        existing_chunk, existing_chunk, chunk
                    )

                # If we come across a summary, ensure the coalesced chunk retains the summary text
                # (useful for Context Manager synthesis later).
                if chunk.summary and not existing_chunk.summary:
                    existing_chunk.summary = chunk.summary

    # Convert the coalesced dictionary back to a list
    merged_list = list(merged_dict.values())

    # Sort the unified candidate pool by score descending
    merged_list.sort(key=lambda x: x.score, reverse=True)

    total_in = sum(len(p) for p in pools if p)
    logger.debug(
        f"Merged {total_in} total chunks into {len(merged_list)} unique candidates."
    )

    return merged_list

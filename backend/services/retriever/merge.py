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


def merge_pools(*pools: List[SourceChunk]) -> List[SourceChunk]:
    """
    Merges multiple candidate pools of SourceChunk objects into a single, deduplicated pool.

    Deduplication and Coalescing Rules:
    - Child hits: Deduplicate by chunk_id natively, but group by parent_id for HRAG.
    - Summary hits: Deduplicate by parent_id.
    - Coalescence: If a summary and one or more child hits share the same parent_id,
      they are coalesced into a single candidate representing that parent.
    - The highest vector similarity score among the coalesced chunks is retained.
    """
    merged_dict = {}

    for pool in pools:
        if not pool:
            continue

        for chunk in pool:
            # Determine the grouping key.
            # If the chunk belongs to a parent (HRAG), we group by parent_id
            # to prevent hydrating the same parent text multiple times.
            # If there is no parent (Naive RAG), we group by its own chunk_id.
            key = chunk.parent_id if chunk.parent_id else chunk.chunk_id

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

                # Keep the maximum score among all child/summary hits for this parent
                if chunk.score > existing_chunk.score:
                    # Inherit the higher-scoring chunk's identity/text, but keep
                    # provenance from every retrieval lane that found this parent.
                    replacement = chunk.model_copy()
                    replacement.source_tier = merged_source_tier
                    replacement.provenance = merged_provenance
                    if existing_chunk.summary and not replacement.summary:
                        replacement.summary = existing_chunk.summary
                    merged_dict[key] = replacement
                    existing_chunk = replacement
                else:
                    existing_chunk.source_tier = merged_source_tier
                    existing_chunk.provenance = merged_provenance

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

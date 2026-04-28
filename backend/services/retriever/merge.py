import logging
from typing import List

from models.schemas import SourceChunk

logger = logging.getLogger(__name__)


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

                # Keep the maximum score among all child/summary hits for this parent
                if chunk.score > existing_chunk.score:
                    existing_chunk.score = chunk.score
                    # Inherit the higher-scoring chunk's identity, but ensure
                    # we don't lose the parent_id reference.
                    existing_chunk.chunk_id = chunk.chunk_id
                    existing_chunk.text = chunk.text
                    existing_chunk.source_tier = chunk.source_tier

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

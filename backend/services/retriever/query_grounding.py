"""Deterministic query-concept coverage helpers for retrieval.

These helpers are intentionally lexical and bounded. They do not decide what
to answer; they only keep the evidence packet honest when vector/rerank scores
surface candidates that do not touch the user's core concepts.
"""

from __future__ import annotations

from typing import Iterable

from models.schemas import SourceChunk
from services.facets.runtime import metadata_facet_terms
from services.retriever.query_semantics import (
    ConceptGroup,
    concept_groups,
    group_matches_text,
)


def chunk_grounding_text(chunk: SourceChunk) -> str:
    """Build a compact, metadata-aware haystack for final-source grounding."""

    metadata = chunk.metadata or {}
    facet_terms = metadata_facet_terms(metadata)
    provenance_terms: list[str] = []
    for item in chunk.provenance or []:
        provenance_terms.extend(
            str(item.get(key) or "")
            for key in (
                "entity",
                "surface_form",
                "predicate",
                "relation_family",
                "domain_type",
                "canonical_family",
                "entity_type",
            )
        )

    parts: Iterable[str] = (
        chunk.text or "",
        chunk.summary or "",
        chunk.doc_name or "",
        chunk.doc_id or "",
        " ".join(chunk.heading_path or []),
        " ".join(facet_terms),
        " ".join(provenance_terms),
    )
    return "\n".join(part for part in parts if part)


def chunk_concept_hits(
    chunk: SourceChunk,
    groups: list[ConceptGroup],
) -> tuple[int, tuple[str, ...]]:
    """Return the number and keys of query concepts covered by a chunk."""

    if not groups:
        return 0, ()
    text = chunk_grounding_text(chunk)
    matched = tuple(group.key for group in groups if group_matches_text(group, text))
    return len(matched), matched

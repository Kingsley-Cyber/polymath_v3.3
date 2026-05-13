"""
Pt 8b — Pydantic Literal-typed schemas for Ghost B extraction output.

These models mirror UNIVERSAL_ENTITY_SCHEMA + UNIVERSAL_RELATION_SCHEMA in
`services.ghost_b` and add strict enum validation via typing.Literal. When
EXTRACTION_STRICT_PYDANTIC_VALIDATION is enabled, the parser validates each
entity/relation through these models AFTER the existing _parse step. Items
that fail validation (off-vocab entity_type or predicate, malformed
confidence, etc.) are dropped instead of soft-remapped to the sentinel.

Idempotency note: when the flag is OFF (the default), nothing imports or
uses these models at runtime; behavior is bit-for-bit identical to the
pre-Pt-8b pipeline. When ON, dropping vs. soft-remapping is the only
behavioral difference — same chunk re-extracted twice still produces the
same canonical ExtractionResult (modulo the LLM's intrinsic non-determinism,
which exists in both modes).

The Literal types are hardcoded here (rather than derived from the lists
in ghost_b.py) so static type-checkers can verify them. Keep them in sync
with UNIVERSAL_ENTITY_SCHEMA + UNIVERSAL_RELATION_SCHEMA when those change.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# Mirror of UNIVERSAL_ENTITY_SCHEMA + the ENTITY_SENTINEL ('other').
# Keep in sync with services/ghost_b.py.
EntityType = Literal[
    "Person",
    "Organization",
    "Location",
    "Event",
    "Concept",
    "Method",
    "Product",
    "Document",
    "Rule",
    "Law",
    "Artifact",
    "TimeReference",
    "other",
]


# Mirror of UNIVERSAL_RELATION_SCHEMA + the RELATION_SENTINEL ('related_to').
# Keep in sync with services/ghost_b.py:UNIVERSAL_RELATION_SCHEMA.
# Pt 8d swap: dropped `classifies` / `runs_on` / `trained_on` (aliased to
# `detects` / `uses` / `uses`); added `defines` / `example_of` / `during`.
Predicate = Literal[
    # Structural
    "part_of",
    "member_of",
    "located_in",
    "works_for",
    "created_by",
    "owns",
    "affiliated_with",
    # Canonicalization
    "synonym_of",
    "instance_of",
    "example_of",           # Pt 8d
    # Operational
    "uses",
    "references",
    "implements",
    "depends_on",
    "produces",
    "stores",
    "detects",
    "supports",
    # Referential / definitional / transformation
    "defines",              # Pt 8d
    "represents",
    "maps_to",
    # Temporal / causal
    "preceded_by",
    "causes",
    "overlaps",
    "during",               # Pt 8d
    # Provenance / conflict
    "derived_from",
    "contradicts",
    "excepts",
    "overrides",
    # Sentinel — anything Ghost B genuinely can't pin down.
    "related_to",
]


class LLMEntity(BaseModel):
    """Strict-validation view of an entity emitted by Ghost B.

    Mirrors `EntityItem` in ghost_b.py but enforces entity_type as a
    Literal. If the LLM (or downstream alias normalization) emits a type
    outside this set, Pydantic raises ValidationError and the item is
    dropped by the caller.
    """

    canonical_name: str = Field(min_length=1, max_length=200)
    surface_form: str = Field(default="", max_length=300)
    entity_type: EntityType
    confidence: float = Field(ge=0.0, le=1.0)


class LLMRelation(BaseModel):
    """Strict-validation view of a relation emitted by Ghost B.

    Mirrors `RelationItem` in ghost_b.py. The predicate Literal is the
    point of this entire Pt 8b layer: it forces the predicate to be one
    of the 30 schema values, blocking the LLM-emitted-then-soft-remapped
    pathway that drove ~21% of edges into the `related_to` bucket.
    """

    subject: str = Field(min_length=1, max_length=200)
    predicate: Predicate
    object: str = Field(min_length=1, max_length=200)
    object_kind: Literal["entity", "literal"] = "literal"
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_phrase: str = Field(default="", max_length=500)
    relation_cue: str = Field(default="", max_length=120)


__all__ = [
    "EntityType",
    "Predicate",
    "LLMEntity",
    "LLMRelation",
]

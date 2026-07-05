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
    "Software",       # Pt9a — libraries, frameworks, apps, APIs, languages, platforms
    "Document",
    "Standard",       # Pt9a — protocols, specifications, data formats, schemas
    "Rule",
    "Law",
    "Artifact",
    "TimeReference",
    "other",
]


# Mirror of UNIVERSAL_RELATION_SCHEMA + the RELATION_SENTINEL ('related_to').
# Keep in sync with services/ghost_b.py:UNIVERSAL_RELATION_SCHEMA.
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
    # Operational
    "uses",
    "runs_on",
    "trained_on",
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

    Pt 10c — added two query-facing fields. Before, the schema captured
    only graph-storage shape (canonical_name + entity_type). That made
    entity-first search (services/retriever/mode_b.py) brittle: substring
    matching the user query against `e.normalized_name` misses common
    variants like "alpha coefficient" → "cronbach alpha". The new fields
    are additive — Ghost B is asked to emit them when natural; both
    default to empty so back-compat with pre-Pt-10c extractions is
    preserved at every layer (Pydantic, dataclass, Neo4j SET, Cypher
    matching via `coalesce(..., [])`).
    """

    canonical_name: str = Field(min_length=1, max_length=200)
    surface_form: str = Field(default="", max_length=300)
    entity_type: EntityType
    confidence: float = Field(ge=0.0, le=1.0)
    # Pt 10c — query-facing variants the LLM should list (abbreviations,
    # synonyms, spelling variants). Mode B's WHERE clause matches against
    # these in addition to normalized_name / display_name.
    query_aliases: list[str] = Field(default_factory=list, max_length=5)
    # Pt 10c — one-sentence "what is this" pulled from the surrounding text.
    # Rendered in chat provenance so the LLM gets immediate semantic
    # context about each cited entity.
    definitional_phrase: str = Field(default="", max_length=200)
    # Pt9b — free-form second-axis facet (library/framework/disorder/...).
    # Distinct from RelationItem.object_kind (which is "entity"|"literal").
    # No Literal enforcement because the natural taxonomy is open-ended;
    # _normalize_object_kind in ghost_b.py canonicalizes variants at parse
    # time against the corpus's schema_lens.object_kinds list. When/if we
    # switch to json_schema mode, the wire format key becomes "e_kind" via
    # Pydantic alias (set when building the schema, not here, so the
    # Python field name stays `object_kind` matching the graph layer's
    # property expectation).
    object_kind: str = Field(default="", max_length=100)


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
    evidence_phrase: str = Field(..., min_length=1, max_length=500)
    relation_cue: str = Field(default="", max_length=120)


# Pt9c — facts schema for json_schema mode. Mirrors FACT_TYPES in
# services/ghost_b.py. Keep in sync.
FactType = Literal[
    "property",
    "status",
    "timestamp",
    "quantity",
    "threshold",
    "category",
    "tag",
    "rule_condition",
    "rule_action",
]


class LLMFact(BaseModel):
    """Strict-validation view of a fact emitted by Ghost B.

    Pt9c — added so the json_schema mode response_format can declare the
    facts envelope alongside entities + relations. The build_json_object_prompt
    has always included a "facts" field in its example shape; this model
    is what makes it physically enforced when json_schema mode is active.
    """

    subject: str = Field(min_length=1, max_length=200)
    fact_type: FactType
    property_name: str = Field(max_length=80)
    value: str = Field(max_length=500)
    unit: str = Field(default="", max_length=40)
    condition: str = Field(default="", max_length=300)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_phrase: str = Field(..., min_length=1, max_length=500)


class ExtractionResponse(BaseModel):
    """Frozen contract — LLM structured output mirrors this shape exactly.

    The json_schema response_format payload is generated from this model
    via .model_json_schema() + the _pin_all_required post-processor in
    ghost_b.py. Adding fields here requires coordinated updates to:

      1. EntityItem / RelationItem / FactItem dataclasses (ghost_b.py)
      2. _parse() to extract any new field
      3. neo4j_writer property maps if the field flows to the graph

    Adding a field in isolation strands data: the LLM emits it,
    _parse() ignores it, the graph never sees it. The entire point of
    json_schema mode is provider-level constraint without changing
    downstream contracts.
    """

    entities: list[LLMEntity] = Field(default_factory=list)
    relations: list[LLMRelation] = Field(default_factory=list)
    facts: list[LLMFact] = Field(default_factory=list)


__all__ = [
    "EntityType",
    "Predicate",
    "FactType",
    "LLMEntity",
    "LLMRelation",
    "LLMFact",
    "ExtractionResponse",
]

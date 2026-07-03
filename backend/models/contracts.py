"""B0 — the five typed contracts (POLYMATH_ARCHITECTURE §2, owner-approved).

The Stage-Contract rule: every produced field names its consumer; every
consumer's field is asserted populated (tests/test_contracts.py is the CI
gate). Writers are meant to accept ONLY these models — this file is the
storage shape, ending the untyped-dict split-brain at the storage boundary.

Composition:
  1. ChunkExtraction  (polymath.extract.v2)  — what extractors emit (local+cloud)
  2. ChunkMetadata                            — identity/provenance, Mongo truth
  3. RetrievalPayload                         — the promotion target (Qdrant, indexed)
  4. GraphWriteModel                          — Neo4j write shape
  5. RerankerInput                            — the short text actually scored
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

EXTRACT_SCHEMA_VERSION = "polymath.extract.v2"
PROMOTE_VERSION = "polymath.promote.v1"


# ── 1. ExtractionOutput — one envelope for BOTH extractors ─────────────────
class ExtractedEntity(BaseModel):
    canonical_name: str = Field(min_length=1, max_length=200)
    surface_form: str = Field(default="", max_length=300)
    entity_type: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    query_aliases: list[str] = Field(default_factory=list)
    definitional_phrase: str = Field(default="", max_length=200)
    object_kind: str = Field(default="", max_length=100)
    char_start: Optional[int] = None  # local emits; cloud may null
    char_end: Optional[int] = None
    # promote-time (ontology resolution at write; null at extract)
    entity_id: Optional[str] = None          # "entity:{slug}" — the graph join key
    domain_type: Optional[str] = None
    canonical_family: Optional[str] = None


class ExtractedRelation(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1, max_length=200)
    object_kind: Literal["entity", "literal"] = "entity"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_phrase: str = Field(default="", max_length=500)
    relation_cue: str = Field(default="", max_length=120)
    # promote-time
    relation_family: Optional[str] = None
    source_predicate: Optional[str] = None
    validation_status: Optional[str] = None


class ExtractedFact(BaseModel):
    subject: str
    fact_type: Literal[
        "property", "status", "timestamp", "quantity", "threshold",
        "category", "tag", "rule_condition", "rule_action",
    ]
    property_name: str = Field(max_length=80)
    value: str = Field(max_length=500)
    unit: Optional[str] = None
    condition: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_phrase: str = Field(default="", max_length=500)
    fact_id: Optional[str] = None            # deterministic hash (Neo4j key)


class ChunkExtraction(BaseModel):
    """Per-CHILD-chunk envelope, keyed (corpus_id, doc_id, chunk_id).
    `extractor` is the ONLY field that differs between local and cloud."""

    schema_version: Literal["polymath.extract.v2"] = EXTRACT_SCHEMA_VERSION
    extractor: Literal["gliner_glirel_local", "cloud_llm"]
    corpus_id: str
    doc_id: str
    chunk_id: str
    parent_id: str
    text: str = ""
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relations: list[ExtractedRelation] = Field(default_factory=list)
    facts: list[ExtractedFact] = Field(default_factory=list)
    schema_lens_id: Optional[str] = None


# ── 2. ChunkMetadata — identity & provenance (Mongo source of truth) ───────
class ChunkMetadata(BaseModel):
    doc_id: str
    chunk_id: str
    parent_id: str
    corpus_id: str
    user_id: str = ""
    source_title: str = ""                   # M2 (title)
    author_or_org: str = ""                  # M2
    source_type: str = ""                    # M2 format-family / Ghost-A refined
    document_date: Optional[str] = None      # M2, ISO
    section_path: list[str] = Field(default_factory=list)  # heading_path
    chunk_kind: str = "body"
    token_count: int = 0
    ingested_at: Optional[str] = None
    document_status: str = "active"
    is_latest: bool = True
    supersedes: list[str] = Field(default_factory=list)
    superseded_by: Optional[str] = None


# ── 3. RetrievalPayload — the promotion target (Qdrant payload; indexed) ───
class RetrievalPayload(BaseModel):
    """ONLY fields used to filter / route / rank. Small, indexed, no free text
    beyond what a filter needs. Payload index ships in the SAME migration as
    the field (CI asserts index presence before a filter may use it)."""

    chunk_id: str
    parent_id: str
    doc_id: str
    corpus_id: str
    user_id: str = ""
    chunk_type: Literal["child", "summary", "doc_summary"] = "child"
    chunk_kind: str = "body"
    language: Optional[str] = None
    domain: Optional[str] = None             # SOFT boost, never a gate
    topic_key: Optional[str] = None          # owner compact schema
    # promoted from Ghost B (B2)
    concepts: list[str] = Field(default_factory=list)       # names+aliases (recall)
    entity_ids: list[str] = Field(default_factory=list)     # entity:{slug} (graph join)
    entity_families: list[str] = Field(default_factory=list)
    entity_domains: list[str] = Field(default_factory=list)
    relation_predicates: list[str] = Field(default_factory=list)
    relation_families: list[str] = Field(default_factory=list)
    fact_types: list[str] = Field(default_factory=list)
    has_relations: bool = False
    # promoted from Ghost A (B3)
    semantic_chunk_type: Optional[str] = None  # definition|claim|procedure|principle|...
    mechanisms: list[str] = Field(default_factory=list)
    key_terms: list[str] = Field(default_factory=list)
    # temporal / versioning (M2)
    document_status: str = "active"
    is_latest: bool = True
    document_date: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    # migration stamps — never version-blind again
    extract_schema_version: str = EXTRACT_SCHEMA_VERSION
    promote_version: str = PROMOTE_VERSION


# ── 4. GraphWriteModel — Neo4j write shape ──────────────────────────────────
class GraphEntity(BaseModel):
    entity_id: str                            # entity:{slug} — GLOBAL, never corpus-prefixed
    canonical_name: str
    entity_type: str = ""
    object_kind: str = ""
    canonical_family: Optional[str] = None
    domain_type: Optional[str] = None
    corpus_ids: list[str] = Field(default_factory=list)  # accumulated union (isolation w/o identity split)


class GraphRelation(BaseModel):
    subject_id: str
    predicate: str
    object_id: str
    relation_family: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_phrase: str = ""
    chunk_id: str = ""                        # provenance


class GraphWriteModel(BaseModel):
    entities: list[GraphEntity] = Field(default_factory=list)
    relations: list[GraphRelation] = Field(default_factory=list)
    facts: list[ExtractedFact] = Field(default_factory=list)


# ── 5. RerankerInput — the short text actually scored ──────────────────────
class RerankerInput(BaseModel):
    """No ids/hashes/paths ever reach the reranker or the answer model."""

    source_book: str = ""
    section: str = ""
    excerpt: str

    def render(self) -> str:
        prefix = ""
        if self.source_book and self.section:
            prefix = f"{self.source_book} › {self.section}\n"
        elif self.source_book:
            prefix = f"{self.source_book}\n"
        return prefix + self.excerpt

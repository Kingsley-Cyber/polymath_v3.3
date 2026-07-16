"""
Neo4j writer — persists graph data after GHOST B extraction (Phase 4).

All upserts use MERGE (idempotent).
All Document/Chunk MATCH clauses scope by corpus_id to prevent cross-corpus bleed.

Entity nodes are global by design — cross-corpus dedup via deterministic ID.

Phase 14.4: entity_id format is now `entity:{name_slug}`. Ghost B's
entity_type is preserved as extraction evidence (`observed_entity_types` on
the node, `extracted_type` on MENTIONS) instead of fragmenting identity.

Entry point: write_document_graph() — call after GHOST B returns ExtractionResult list.
"""

import asyncio
import hashlib
import logging
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from neo4j import AsyncDriver
from neo4j.exceptions import TransientError

from services.ghost_b import (
    EntityItem,
    ExtractionResult,
    FactItem,
    RelationItem,
    SchemaContext,
    UNIVERSAL_RELATION_SCHEMA,
    normalize_relation_predicate_alias,
)
from services.graph.entity_cleaning import is_junk_extracted_entity
from services.graph.entity_dedup.resolve import resolve_entity_ids

logger = logging.getLogger(__name__)
ALIAS_MAP_PATH = Path(__file__).with_name("entity_aliases.json")
FACET_TAXONOMY_PATH = Path(__file__).with_name("facet_taxonomy.json")
DOMAIN_TAXONOMY_PATH = Path(__file__).with_name("domain_taxonomy.json")
CANONICAL_FAMILIES_PATH = Path(__file__).with_name("canonical_families.json")
ENTITY_TYPE_OVERRIDES_PATH = Path(__file__).with_name("entity_type_overrides.json")
ONTOLOGY_VERSION = "2026-04-25-v3"
ENTITY_ID_PREFIX = "entity"
GRAPH_PROMOTE_VERSION = "polymath.promote.v1"
GRAPH_WRITE_MAX_ATTEMPTS = 3
GRAPH_WRITE_DEADLOCK_BACKOFF_SECONDS = 0.75
# Neo4j transaction-memory safety.  These constants bound every data-sized
# production write/refresh in this module; callers must not replace them with
# a whole-document/corpus list.  Aggregate refresh is intentionally the most
# conservative because its two OPTIONAL MATCH aggregations are the highest-
# memory graph write in the ingestion path.
GRAPH_ENTITY_AGGREGATE_BATCH_SIZE = 100
GRAPH_WRITE_ROW_BATCH_SIZE = 100
GRAPH_DELETE_BATCH_SIZE = 100
GRAPH_RELATION_PRUNE_BATCH_SIZE = 100
IDENTITY_KEY_SEPARATOR = "|"
GENERIC_ENTITY_TERMS = {
    "agent",
    "attention",
    "class",
    "data",
    "example",
    "field",
    "memory",
    "method",
    "model",
    "object",
    "process",
    "system",
    "user",
    "value",
}


def corpus_content_key(corpus_id: str, content_id: str) -> str:
    """Stable, parseable identity for provenance stored on global edges."""

    if IDENTITY_KEY_SEPARATOR in corpus_id or IDENTITY_KEY_SEPARATOR in content_id:
        raise ValueError("corpus/content identity contains reserved separator")
    return f"{corpus_id}{IDENTITY_KEY_SEPARATOR}{content_id}"


ENTITY_TYPE_PRIORITY = [
    # Phase 5 — scoped Roblox/Luau types come BEFORE the generic universal
    # types so an entity observed as both "RobloxService" (from Phase 5's
    # ontology resolver on a Luau chunk) and "Method" (from a graphify
    # symbols_called backfill on the same chunk) resolves to RobloxService.
    # These types are produced only by the scoped resolver
    # (services/graph/roblox_ontology.py) which itself is gated on
    # chunk.language ∈ {lua, luau} OR metadata.roblox_apis non-empty, so
    # they cannot leak into non-Roblox corpora.
    "RobloxService",
    "RobloxClass",
    "RobloxNetworkPrimitive",
    "LuauDataType",
    "Person",
    "Organization",
    "Location",
    "Event",
    "Document",
    "Rule",
    "Law",
    "Product",
    "Artifact",
    "Method",
    "Software",
    "Standard",
    "Concept",
    "TimeReference",
    SchemaContext.ENTITY_SENTINEL,
]
RELATION_FAMILY_MAP = {
    # Families are a retrieval/synthesis lens over the raw Ghost B predicate.
    # They make edge strength legible without replacing the evidence label.
    "part_of": "Structural",
    "member_of": "Structural",
    "uses": "Operational",
    "runs_on": "Operational",
    "trained_on": "Operational",
    "implements": "Operational",
    "depends_on": "Operational",
    "produces": "Operational",
    "references": "Referential",
    "derived_from": "Referential",
    "causes": "Causal",
    "preceded_by": "Causal",
    "overlaps": "Causal",
    "contradicts": "Conflict",
    "excepts": "Conflict",
    "overrides": "Conflict",
    "created_by": "Provenance",
    "works_for": "Affiliation",
    "owns": "Affiliation",
    "affiliated_with": "Affiliation",
    "located_in": "Spatial",
    "synonym_of": "Canonicalization",
    "instance_of": "Canonicalization",
    "stores": "Operational",
    "detects": "Operational",
    "supports": "Operational",
    "represents": "Referential",
    "maps_to": "Referential",
    # Compatibility for historical graph edges from earlier extraction
    # schemas. New ingestions normalize examples into instance_of and temporal
    # containment into overlaps, but old edges can still be read and grouped.
    "defines": "Referential",
    "example_of": "Canonicalization",
    "during": "Causal",
    "related_to": "WeakAssociation",
}
_APPROVED_SPECIFIC_RELATIONS = {
    value
    for value in UNIVERSAL_RELATION_SCHEMA
    if value != SchemaContext.RELATION_SENTINEL
}
_ONTOLOGY_ALLOWED_PAIR_RAW: dict[str, tuple[tuple[str, str], ...]] = {
    # Mirrored from config/ontology.yaml. The backend Docker build context is
    # backend/, so this writer cannot rely on reading the repo-level YAML at
    # runtime. Keep the raw legacy names here and normalize through
    # normalize_relation_predicate_alias() below.
    "includes": (
        ("Concept", "Concept"),
        ("Concept", "Method"),
        ("Concept", "Software"),
        ("Software", "Method"),
        ("Software", "Artifact"),
        ("Product", "Artifact"),
        ("Document", "Concept"),
    ),
    "uses": (
        ("Person", "Software"),
        ("Person", "Method"),
        ("Organization", "Software"),
        ("Organization", "Method"),
        ("Software", "Software"),
        ("Software", "Method"),
        ("Software", "Standard"),
        ("Software", "Artifact"),
        ("Method", "Software"),
        ("Method", "Concept"),
        ("Concept", "Method"),
        ("Concept", "Software"),
        ("Artifact", "Software"),
    ),
    "supports": (
        ("Software", "Method"),
        ("Software", "Concept"),
        ("Software", "Artifact"),
        ("Artifact", "Method"),
        ("Artifact", "Concept"),
        ("Method", "Concept"),
        ("Concept", "Method"),
        ("Organization", "Software"),
        ("Product", "Method"),
    ),
    "produces": (
        ("Software", "Artifact"),
        ("Software", "Document"),
        ("Software", "Concept"),
        ("Method", "Artifact"),
        ("Method", "Concept"),
        ("Person", "Document"),
        ("Organization", "Product"),
        ("Product", "Artifact"),
    ),
    "implements": (
        ("Software", "Method"),
        ("Software", "Standard"),
        ("Software", "Rule"),
        ("Artifact", "Method"),
        ("Artifact", "Standard"),
        ("Method", "Concept"),
        ("Product", "Method"),
    ),
    "has_part": (
        ("Concept", "Concept"),
        ("Concept", "Method"),
        ("Software", "Software"),
        ("Software", "Artifact"),
        ("Product", "Artifact"),
        ("Document", "Concept"),
        ("Organization", "Organization"),
    ),
    "instance_of": (
        ("Software", "Concept"),
        ("Product", "Concept"),
        ("Method", "Concept"),
        ("Artifact", "Concept"),
        ("Person", "Concept"),
        ("Organization", "Concept"),
        ("Document", "Concept"),
    ),
    "references": (
        ("Document", "Document"),
        ("Document", "Concept"),
        ("Software", "Document"),
        ("Software", "Standard"),
        ("Method", "Document"),
        ("Concept", "Document"),
        ("Person", "Document"),
        ("Organization", "Document"),
    ),
    "depends_on": (
        ("Software", "Software"),
        ("Software", "Artifact"),
        ("Software", "Standard"),
        ("Method", "Software"),
        ("Method", "Concept"),
        ("Concept", "Concept"),
        ("Product", "Software"),
    ),
    "causes": (
        ("Concept", "Concept"),
        ("Concept", "Method"),
        ("Method", "Concept"),
        ("Software", "Concept"),
        ("Event", "Event"),
        ("Rule", "Concept"),
    ),
    "synonym_of": (
        ("Concept", "Concept"),
        ("Method", "Method"),
        ("Software", "Software"),
        ("Artifact", "Artifact"),
        ("Standard", "Standard"),
        ("Product", "Product"),
    ),
    "member_of": (
        ("Person", "Organization"),
        ("Organization", "Organization"),
        ("Software", "Concept"),
        ("Product", "Concept"),
        ("Concept", "Concept"),
    ),
    "example_of": (
        ("Software", "Concept"),
        ("Product", "Concept"),
        ("Method", "Concept"),
        ("Artifact", "Concept"),
        ("Document", "Concept"),
        ("Person", "Concept"),
    ),
    "deploys": (
        ("Person", "Software"),
        ("Organization", "Software"),
        ("Software", "Location"),
        ("Software", "Artifact"),
        ("Method", "Software"),
        ("Product", "Software"),
    ),
    "creates": (
        ("Person", "Document"),
        ("Person", "Product"),
        ("Person", "Software"),
        ("Organization", "Product"),
        ("Organization", "Software"),
        ("Software", "Artifact"),
        ("Method", "Artifact"),
    ),
    "trains": (
        ("Person", "Method"),
        ("Organization", "Method"),
        ("Method", "Software"),
        ("Software", "Software"),
        ("Concept", "Software"),
        ("Artifact", "Software"),
    ),
    "runs": (
        ("Software", "Software"),
        ("Software", "Location"),
        ("Software", "Artifact"),
        ("Product", "Software"),
        ("Method", "Software"),
    ),
    "located_in": (
        ("Person", "Location"),
        ("Organization", "Location"),
        ("Software", "Location"),
        ("Product", "Location"),
        ("Artifact", "Location"),
        ("Concept", "Location"),
    ),
}
_ONTOLOGY_PREDICATE_COMPAT_ALIASES = {
    # Legacy ontology predicates that predate the current universal schema.
    "deploys": ("runs_on", False),
    "runs": ("runs_on", False),
    "trains": ("trained_on", False),
    "example_of": ("instance_of", False),
}
_ONTOLOGY_PAIR_PROMOTION_DENY = {
    # Same-type pairs can be uniquely legal as synonym_of in config/ontology.yaml,
    # but synonymy requires textual alias evidence. Do not infer it from type
    # compatibility alone.
    "synonym_of",
}
_OPERATIONAL_SUBJECT_DOMAINS = {
    "Feature",
    "Module",
    "Screen",
    "Product",
    "ArchitectureDecision",
    "AIModel",
    "CloudService",
    "Database",
    "MobileApp",
}
_OPERATIONAL_OBJECT_DOMAINS = {
    "AIModel",
    "CloudService",
    "DataObject",
    "Database",
    "Dataset",
    "Device",
    "Platform",
}
_OPERATIONAL_OBJECT_KINDS = {
    "App",
    "DataObject",
    "Dataset",
    "Framework",
    "Library",
    "Model",
    "Service",
    "Tool",
    "API",
    "Database",
}
_OUTPUT_OBJECT_DOMAINS = {"OutputArtifact"}
_OUTPUT_OBJECT_KINDS = {"Book", "Report", "Spec", "Whitepaper"}
_CONSTRAINT_OBJECT_DOMAINS = {"Constraint", "Risk", "PricingRule"}
_PRODUCTION_HINTS = (
    "generation",
    "generator",
    "synthesis",
    "pipeline",
    "engine",
    "export",
    "output",
    "producer",
)
_RELATION_CUE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "trained_on",
        ("trained on", "trained with", "training data", "training set", "learns from"),
    ),
    (
        "runs_on",
        ("runs on", "run on", "executes on", "deployed on", "on-device", "on device"),
    ),
    (
        "stores",
        ("stores", "stored in", "persists", "persisted in", "saves to", "saved to"),
    ),
    # `extracts` was merged into `detects`; both verb classes route here so
    # cue-based predicate inference produces a single canonical edge label.
    (
        "detects",
        (
            "detects",
            "identifies",
            "recognizes",
            "finds",
            "object detection",
            "extracts",
            "extract ",
            "extracted from",
            "feature extraction",
            "entity extraction",
            "pulls from",
        ),
    ),
    (
        "classifies",
        ("classifies", "classification", "predicts", "assigns category", "labels as"),
    ),
    # `calls` was merged into `uses`; the API-invocation cues route to `uses`.
    (
        "uses",
        (
            "uses",
            "using",
            "utilizes",
            "consumes",
            "powered by",
            "calls",
            "invokes",
            "requests",
            "queries",
            "api call",
            "endpoint",
        ),
    ),
    # New canonicalization / typing / affiliation cues.
    ("synonym_of", ("aka", "also known as", "same as", "alias", "synonym")),
    (
        "instance_of",
        ("is a kind of", "is a type of", "is an instance of", "subclass of"),
    ),
    ("owns", ("owns", "owned by", "holds title to")),
    (
        "affiliated_with",
        ("affiliated with", "associated with", "partner of", "sponsored by"),
    ),
    ("overlaps", ("overlaps with", "concurrent with", "co-occurs with", "during")),
    ("maps_to", ("maps to", "maps onto", "converts", "transforms", "translates")),
    ("represents", ("represents", "models", "modeled as", "encodes")),
    ("supports", ("supports", "enables", "allows", "provides", "facilitates")),
    ("produces", ("produces", "generates", "outputs", "emits", "returns", "creates")),
    ("depends_on", ("depends on", "requires", "prerequisite", "constraint", "needs")),
    # `uses` cue tuple is defined above (absorbing the legacy `calls` cues);
    # the duplicate plain-`uses` entry that lived here has been removed.
    ("implements", ("implements", "realizes", "embodies", "concrete form")),
    ("references", ("references", "cites", "mentions", "according to", "described in")),
    (
        "derived_from",
        ("derived from", "based on", "adapted from", "inspired by", "built on"),
    ),
    ("causes", ("causes", "leads to", "results in", "because of")),
    ("preceded_by", ("preceded by", "after", "followed by")),
    ("contradicts", ("contradicts", "conflicts with", "inconsistent with", "opposes")),
    ("excepts", ("except", "unless", "excluding", "exception")),
    ("overrides", ("overrides", "replaces", "supersedes", "deprecated by")),
)
_RECOVERABLE_SOURCE_PREDICATES = {
    "part_of",
    "member_of",
    "owns",
    "affiliated_with",
    "synonym_of",
    "instance_of",
    "uses",  # absorbs legacy `calls`
    "references",
    "implements",
    "depends_on",
    "produces",
    "stores",
    "detects",  # absorbs legacy `extracts`
    "classifies",
    "runs_on",
    "trained_on",
    "supports",
    "represents",
    "maps_to",
    "preceded_by",
    "causes",
    "overlaps",
    "derived_from",
    "contradicts",
    "excepts",
    "overrides",
}


def relation_family_for_predicate(predicate: str | None) -> str:
    """Return the stable relation family for a raw extraction predicate.

    The raw predicate is still stored on the edge. The family is a deterministic
    grouping used by graph retrieval and Mission Control to distinguish strong
    operational/structural/referential edges from weak catch-all associations.
    """
    normalized = str(predicate or "").strip()
    return RELATION_FAMILY_MAP.get(normalized, "WeakAssociation")


@dataclass(frozen=True)
class RelationEdgeMitigation:
    """Query-facing metadata that keeps weak fallback edges bounded and useful."""

    edge_state: str
    relation_family: str
    fallback: bool
    fallback_family: str
    candidate_predicates: list[str]
    candidate_scores: list[float]
    candidate_score_sources: list[str]
    promoted_by: str
    fallback_evidence_phrase: str
    related_to_query_weight: float
    related_to_max_hops: int


def _identity_value(identity: dict | None, key: str) -> str:
    if not identity:
        return ""
    return str(identity.get(key) or "").strip()


def _identity_text(identity: dict | None) -> str:
    if not identity:
        return ""
    parts = [
        identity.get("canonical_name"),
        identity.get("display_name"),
        identity.get("primary_entity_type"),
        identity.get("object_kind"),
        identity.get("domain_type"),
        identity.get("canonical_family"),
    ]
    return " ".join(str(p).lower() for p in parts if p)


def _predicate_from_evidence(*parts: str | None) -> str | None:
    text = " ".join(str(part or "").lower() for part in parts if part)
    if not text:
        return None
    for predicate, cues in _RELATION_CUE_PATTERNS:
        if any(cue in text for cue in cues):
            return predicate
    return None


def _normalized_specific_predicate(predicate: str | None) -> str | None:
    normalized, _reverse = normalize_relation_predicate_alias(
        str(predicate or "").strip()
    )
    if normalized in _APPROVED_SPECIFIC_RELATIONS:
        return normalized
    return None


def _normalize_ontology_predicate(predicate: str) -> tuple[str, bool]:
    key = re.sub(r"[^a-z0-9]+", "_", str(predicate or "").lower()).strip("_")
    if key in _ONTOLOGY_PREDICATE_COMPAT_ALIASES:
        return _ONTOLOGY_PREDICATE_COMPAT_ALIASES[key]
    return normalize_relation_predicate_alias(predicate)


@lru_cache(maxsize=1)
def _ontology_unique_predicate_by_pair() -> dict[tuple[str, str], str]:
    candidates: dict[tuple[str, str], set[str]] = {}
    for raw_predicate, pairs in _ONTOLOGY_ALLOWED_PAIR_RAW.items():
        predicate, reverse = _normalize_ontology_predicate(raw_predicate)
        if (
            predicate not in _APPROVED_SPECIFIC_RELATIONS
            or predicate in _ONTOLOGY_PAIR_PROMOTION_DENY
        ):
            continue
        for subject_type, object_type in pairs:
            pair = (
                (object_type, subject_type) if reverse else (subject_type, object_type)
            )
            candidates.setdefault(pair, set()).add(predicate)
    return {
        pair: next(iter(predicates))
        for pair, predicates in candidates.items()
        if len(predicates) == 1
    }


def _promote_related_to_by_unique_ontology_pair(
    subject_identity: dict | None,
    object_identity: dict | None,
) -> str | None:
    subject_type = _identity_value(subject_identity, "primary_entity_type")
    object_type = _identity_value(object_identity, "primary_entity_type")
    if not subject_type or not object_type:
        return None
    return _ontology_unique_predicate_by_pair().get((subject_type, object_type))


def _candidate_predicate_records(
    *,
    source_predicate: str | None,
    evidence_phrase: str | None,
    relation_cue: str | None,
    confidence: float,
) -> list[tuple[str, float, str]]:
    """Recover deterministic candidate predicates for surviving fallback edges.

    GLiREL/LLM validation can demote a relation to `related_to`; the original
    predicate and evidence cue are still partial signal. Store those signals as
    primitive arrays so Neo4j can persist them directly on the relationship.
    """

    candidates: list[tuple[str, float, str]] = []
    seen: set[str] = set()

    def add(predicate: str | None, source: str, score: float) -> None:
        normalized = _normalized_specific_predicate(predicate)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append((normalized, round(max(0.0, min(1.0, score)), 4), source))

    cue_predicate = _predicate_from_evidence(evidence_phrase, relation_cue)
    add(cue_predicate, "evidence_cue", max(float(confidence or 0.0), 0.55))
    add(source_predicate, "source_predicate", float(confidence or 0.0))
    return candidates


def build_relation_edge_mitigation(
    *,
    extracted_predicate: str,
    refined_predicate: str,
    source_predicate: str | None,
    confidence: float,
    evidence_phrase: str | None,
    relation_cue: str | None,
    predicate_refined: bool,
) -> RelationEdgeMitigation:
    """Classify a relation edge for bounded fallback use at write/query time.

    The predicate remains honest. When an edge is still `related_to`, this
    helper records the partial family/candidate/evidence signal needed for
    later refinement and ensures query traversal treats it as recall, not trust.
    """

    sentinel = SchemaContext.RELATION_SENTINEL
    evidence = str(evidence_phrase or "").strip()
    candidates = _candidate_predicate_records(
        source_predicate=source_predicate,
        evidence_phrase=evidence_phrase,
        relation_cue=relation_cue,
        confidence=confidence,
    )

    if refined_predicate != sentinel:
        edge_state = (
            "refined"
            if predicate_refined and extracted_predicate == sentinel
            else "typed"
        )
        return RelationEdgeMitigation(
            edge_state=edge_state,
            relation_family=relation_family_for_predicate(refined_predicate),
            fallback=False,
            fallback_family="",
            candidate_predicates=[p for p, _score, _source in candidates],
            candidate_scores=[score for _p, score, _source in candidates],
            candidate_score_sources=[source for _p, _score, source in candidates],
            promoted_by=(
                "deterministic_related_to_refinement" if edge_state == "refined" else ""
            ),
            fallback_evidence_phrase="",
            related_to_query_weight=1.0,
            related_to_max_hops=2,
        )

    candidate_families = {
        relation_family_for_predicate(predicate)
        for predicate, _score, _source in candidates
        if relation_family_for_predicate(predicate) != "WeakAssociation"
    }
    fallback_family = (
        next(iter(candidate_families)) if len(candidate_families) == 1 else ""
    )
    relation_family = fallback_family or relation_family_for_predicate(
        refined_predicate
    )
    return RelationEdgeMitigation(
        edge_state="family" if fallback_family else "fallback",
        relation_family=relation_family,
        fallback=True,
        fallback_family=fallback_family,
        candidate_predicates=[p for p, _score, _source in candidates],
        candidate_scores=[score for _p, score, _source in candidates],
        candidate_score_sources=[source for _p, _score, source in candidates],
        promoted_by="",
        fallback_evidence_phrase=evidence,
        related_to_query_weight=0.5,
        related_to_max_hops=1,
    )


def _identity_domain_kind_type(identity: dict | None) -> tuple[str, str, str]:
    return (
        _identity_value(identity, "domain_type"),
        _identity_value(identity, "object_kind"),
        _identity_value(identity, "primary_entity_type"),
    )


def _has_any(value: str, candidates: set[str]) -> bool:
    return bool(value and value in candidates)


def _relation_compatible_with_facets(
    predicate: str,
    subject_identity: dict | None,
    object_identity: dict | None,
) -> bool:
    """Cheap ontology-aware compatibility for repairing soft-remapped edges.

    This is deliberately looser than Ghost B's broad entity-type domain/range
    map because the writer has richer facets. It is still conservative enough
    to avoid turning arbitrary `related_to` edges into confident facts.
    """
    if predicate not in _APPROVED_SPECIFIC_RELATIONS:
        return False
    if not subject_identity or not object_identity:
        return False

    subject_domain, subject_kind, subject_type = _identity_domain_kind_type(
        subject_identity
    )
    object_domain, object_kind, object_type = _identity_domain_kind_type(
        object_identity
    )
    operational_subject = (
        _has_any(subject_domain, _OPERATIONAL_SUBJECT_DOMAINS)
        or subject_type in {"Artifact", "Method", "Organization", "Product"}
        or subject_kind in {"App", "Library", "Service", "Tool", "API", "Framework"}
    )
    operational_object = (
        _has_any(object_domain, _OPERATIONAL_OBJECT_DOMAINS)
        or _has_any(object_kind, _OPERATIONAL_OBJECT_KINDS)
        or object_type in {"Artifact", "Method", "Product"}
    )

    if predicate in {"uses", "supports"}:
        return operational_subject and operational_object
    if predicate == "runs_on":
        return operational_subject and (
            object_domain in {"Device", "Platform", "CloudService"}
            or object_kind in {"Device", "Platform", "Service", "Framework"}
            or object_type in {"Artifact", "Product", "Organization", "Location"}
        )
    if predicate == "trained_on":
        return operational_subject and (
            object_domain in {"Dataset", "DataObject"}
            or object_kind in {"Dataset", "DataObject"}
            or object_type in {"Artifact", "Concept", "Document", "Product"}
        )
    if predicate == "stores":
        return operational_subject and (
            object_domain in {"DataObject", "Dataset", "OutputArtifact"}
            or object_kind in {"DataObject", "Dataset", "Document", "Report", "Spec"}
            or object_type in {"Artifact", "Concept", "Document", "Product"}
        )
    if predicate in {"detects", "classifies"}:
        return operational_subject and object_type in {
            "Artifact",
            "Concept",
            "Document",
            "Event",
            "Location",
            "Organization",
            "Person",
            "Product",
        }
    if predicate == "produces":
        return operational_subject and (
            object_domain in _OUTPUT_OBJECT_DOMAINS | {"DataObject", "Dataset"}
            or object_kind in _OUTPUT_OBJECT_KINDS | {"Dataset", "DataObject"}
            or object_type
            in {"Artifact", "Concept", "Document", "Event", "Method", "Product"}
        )
    if predicate == "depends_on":
        return object_domain in _CONSTRAINT_OBJECT_DOMAINS or object_type in {
            "Artifact",
            "Concept",
            "Document",
            "Law",
            "Method",
            "Product",
            "Rule",
        }
    if predicate == "implements":
        return operational_subject and object_type in {
            "Concept",
            "Method",
            "Rule",
            "Law",
        }
    if predicate in {"references", "derived_from", "represents", "maps_to"}:
        return object_type in {
            "Artifact",
            "Concept",
            "Document",
            "Event",
            "Method",
            "Organization",
            "Person",
            "Product",
            "Rule",
            "Law",
        }
    if predicate in {"part_of", "member_of", "created_by", "works_for", "located_in"}:
        return True
    if predicate in {"causes", "preceded_by", "contradicts", "excepts", "overrides"}:
        return object_type in {
            "Concept",
            "Document",
            "Event",
            "Law",
            "Method",
            "Rule",
            "TimeReference",
        }
    return False


def _recover_source_predicate_with_evidence(
    predicate: str,
    subject_identity: dict | None,
    object_identity: dict | None,
    evidence_phrase: str | None,
) -> bool:
    """Allow high-confidence LLM source intent to survive broad type mismatch.

    Ghost B's broad entity labels are intentionally coarse. A Product/Concept
    mismatch often fails the initial domain/range map even when the evidence
    phrase is clear. We recover only non-identity predicates here; strict
    identity predicates such as `works_for`/`created_by` still need proper
    facets or direction repair.
    """
    if predicate not in _RECOVERABLE_SOURCE_PREDICATES:
        return False
    if not subject_identity or not object_identity:
        return False
    if not str(evidence_phrase or "").strip():
        return False
    return True


def refine_related_to_predicate(
    predicate: str,
    subject_identity: dict | None,
    object_identity: dict | None,
    *,
    source_predicate: str | None = None,
    evidence_phrase: str | None = None,
    relation_cue: str | None = None,
) -> str:
    """Conservatively refine a weak `related_to` edge using ontology facets.

    Category B justification: Ghost B is intentionally recall-friendly and
    domain/range validation remaps uncertain relations to `related_to`. At
    ingestion time we now have deterministic facets (`domain_type`,
    `object_kind`, `canonical_family`) that can recover a small number of
    obvious relations without another LLM call. If the facets do not make the
    edge plain, the weak association is preserved.
    """
    if predicate != SchemaContext.RELATION_SENTINEL:
        return predicate
    if not subject_identity or not object_identity:
        return predicate

    evidence_predicate = _predicate_from_evidence(evidence_phrase, relation_cue)
    if evidence_predicate and _relation_compatible_with_facets(
        evidence_predicate, subject_identity, object_identity
    ):
        return evidence_predicate

    original_predicate = str(source_predicate or "").strip()
    original_predicate, _ = normalize_relation_predicate_alias(original_predicate)
    if (
        original_predicate in _APPROVED_SPECIFIC_RELATIONS
        and _relation_compatible_with_facets(
            original_predicate, subject_identity, object_identity
        )
    ):
        return original_predicate
    if _recover_source_predicate_with_evidence(
        original_predicate,
        subject_identity,
        object_identity,
        evidence_phrase,
    ):
        return original_predicate

    ontology_pair_predicate = _promote_related_to_by_unique_ontology_pair(
        subject_identity,
        object_identity,
    )
    if ontology_pair_predicate:
        return ontology_pair_predicate

    subject_domain = _identity_value(subject_identity, "domain_type")
    object_domain = _identity_value(object_identity, "domain_type")
    subject_type = _identity_value(subject_identity, "primary_entity_type")
    object_type = _identity_value(object_identity, "primary_entity_type")
    object_kind = _identity_value(object_identity, "object_kind")
    subject_text = _identity_text(subject_identity)

    if object_domain in _CONSTRAINT_OBJECT_DOMAINS or object_type in {"Rule", "Law"}:
        return "depends_on"

    if object_domain in _OUTPUT_OBJECT_DOMAINS or object_kind in _OUTPUT_OBJECT_KINDS:
        if subject_domain in _OPERATIONAL_SUBJECT_DOMAINS or subject_type in {
            "Method",
            "Product",
            "Artifact",
            "Organization",
        }:
            return "produces"

    if (
        object_domain == "DataObject"
        and any(hint in subject_text for hint in _PRODUCTION_HINTS)
        and subject_domain in _OPERATIONAL_SUBJECT_DOMAINS | {"OutputArtifact"}
    ):
        return "produces"

    if (
        object_domain in _OPERATIONAL_OBJECT_DOMAINS
        or object_kind in _OPERATIONAL_OBJECT_KINDS
    ) and (
        subject_domain in _OPERATIONAL_SUBJECT_DOMAINS
        or subject_type in {"Person", "Organization", "Method", "Product", "Artifact"}
    ):
        return "uses"

    if subject_domain in {"Module", "Screen", "Feature", "ArchitectureDecision"}:
        if object_type in {"Concept", "Method"} and object_domain not in {
            "Constraint",
            "Risk",
            "DataObject",
        }:
            return "implements"

    if subject_type == "Document" and object_type in {
        "Document",
        "Concept",
        "Method",
        "Person",
        "Organization",
        "Rule",
        "Law",
    }:
        return "references"

    return predicate


def relation_edge_strength(
    predicate: str,
    confidence: float,
    validation_status: str | None = None,
    *,
    predicate_refined: bool = False,
) -> str:
    """Classify relation reliability for Mission Control and future filters."""
    status = str(validation_status or "")
    if predicate == SchemaContext.RELATION_SENTINEL:
        return "weak"
    if "domain_range_warn" in status:
        return "thin"
    if predicate_refined:
        return "repaired"
    if "evidence_cue_repair" in status:
        return "repaired"
    if validation_status in {"domain_range_mismatch", "schema_predicate_remap"}:
        return "thin"
    if confidence < 0.6:
        return "thin"
    return "strong"


def relation_eligible_for_synthesis(
    predicate: str,
    confidence: float,
    validation_status: str | None = None,
) -> bool:
    """Keep weak catchall edges available in the graph but out of strong synthesis."""
    if predicate == SchemaContext.RELATION_SENTINEL or confidence < 0.55:
        return False
    if "domain_range_warn" in str(validation_status or ""):
        return confidence >= 0.75
    return True


def normalize_entity_name(name: str) -> str:
    """Canonical form for dedup: lowercase, NFKD, strip punctuation, collapse spaces."""
    name = name.lower().strip()
    name = unicodedata.normalize("NFKD", name)
    name = re.sub(r"[^\w\s]", "", name)
    return re.sub(r"\s+", " ", name).strip()


@lru_cache(maxsize=1)
def _load_alias_lookup() -> dict[str, str]:
    try:
        data = json.loads(ALIAS_MAP_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Entity alias map failed to load: %s", exc)
        return {}

    lookup: dict[str, str] = {}
    for canonical, aliases in data.items():
        canonical_norm = normalize_entity_name(canonical)
        if not canonical_norm:
            continue
        lookup[canonical_norm] = canonical_norm
        for alias in aliases or []:
            alias_norm = normalize_entity_name(str(alias))
            if alias_norm:
                lookup[alias_norm] = canonical_norm
    return lookup


def resolve_entity_alias(normalized_name: str) -> str:
    """Return the configured canonical alias for an already-normalized name."""
    return _load_alias_lookup().get(normalized_name, normalized_name)


def canonicalize_entity_name(name: str) -> str:
    return resolve_entity_alias(normalize_entity_name(name))


@lru_cache(maxsize=1)
def _load_entity_type_overrides() -> dict[str, str]:
    try:
        data = json.loads(ENTITY_TYPE_OVERRIDES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Entity type overrides failed to load: %s", exc)
        return {}
    if not isinstance(data, dict):
        return {}

    overrides: dict[str, str] = {}
    for name, value in data.items():
        canonical = canonicalize_entity_name(str(name))
        if not canonical:
            continue
        if isinstance(value, str):
            overrides[canonical] = value
        elif isinstance(value, dict) and value.get("primary_entity_type"):
            overrides[canonical] = str(value["primary_entity_type"])
    return overrides


def resolve_primary_entity_type(
    canonical_name: str,
    observed_types: list[str],
) -> str:
    """Pick the stable node type for one canonical entity.

    Ghost B's type is evidence from a chunk, not global identity. This resolver
    collapses split nodes such as Product:pvector + Method:pvector into one
    entity:pvector node while preserving all observed types on the node and
    MENTIONS edge.
    """
    canonical = canonicalize_entity_name(canonical_name)
    override = _load_entity_type_overrides().get(canonical)
    if override:
        return override
    observed = {t for t in observed_types if t}
    for candidate in ENTITY_TYPE_PRIORITY:
        if candidate in observed:
            return candidate
    return SchemaContext.ENTITY_SENTINEL


@lru_cache(maxsize=1)
def _load_facet_taxonomy() -> dict:
    try:
        data = json.loads(FACET_TAXONOMY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Facet taxonomy failed to load: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=1)
def _load_domain_taxonomy() -> dict:
    try:
        data = json.loads(DOMAIN_TAXONOMY_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Domain taxonomy failed to load: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=1)
def _load_canonical_families() -> dict:
    try:
        data = json.loads(CANONICAL_FAMILIES_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("Canonical family map failed to load: %s", exc)
        return {}
    return data if isinstance(data, dict) else {}


def _norm_contains(haystack: str, needle: str) -> bool:
    needle_norm = normalize_entity_name(needle)
    if not needle_norm:
        return False
    return re.search(rf"\b{re.escape(needle_norm)}\b", haystack) is not None


def resolve_canonical_family(
    entity_name: str,
    text_context: str = "",
) -> str | None:
    """Resolve a stable cross-type family label from curated aliases.

    Families are deliberately small and auditable. They group entities such as
    Box2D/PBox2D/JBox2D under `physics_simulation` without asking Ghost B to
    produce additional ontology fields.
    """
    families = _load_canonical_families()
    if not families:
        return None
    raw_name = str(entity_name or "")
    name_norm = canonicalize_entity_name(raw_name).replace("_", " ")
    context_norm = normalize_entity_name(text_context)
    haystack = " ".join(part for part in (name_norm, context_norm) if part)
    if not haystack:
        return None

    for family, spec in families.items():
        if not isinstance(spec, dict):
            continue
        for term in [*(spec.get("members") or []), *(spec.get("synonyms") or [])]:
            term_norm = canonicalize_entity_name(str(term)).replace("_", " ")
            if term_norm and (
                name_norm == term_norm or _norm_contains(haystack, term_norm)
            ):
                return str(family)
    return None


def resolve_facets(
    entity_name: str,
    entity_type: str,
    text_context: str = "",
) -> dict[str, str]:
    """Infer lightweight ontology facets for an extracted entity.

    This is the first soft-ontology layer: keep Ghost B's broad entity_type,
    then add an optional object_kind hierarchy for concrete objects such as
    libraries, apps, reports, datasets, and books. It is deterministic and
    ingestion-time only, so query latency stays bounded.
    """
    taxonomy = _load_facet_taxonomy()
    type_taxonomy = taxonomy.get(entity_type)
    if not isinstance(type_taxonomy, dict):
        return {}

    raw_name = str(entity_name or "")
    raw_lower = raw_name.lower().strip()
    name_norm = canonicalize_entity_name(raw_name)
    name_match = name_norm.replace("_", " ")
    context_norm = normalize_entity_name(text_context)
    haystack = " ".join(part for part in (name_match, context_norm) if part)

    def _facet(kind: str, spec: dict) -> dict[str, str]:
        parent = str(spec.get("parent") or entity_type)
        return {
            "object_kind": kind,
            "object_kind_parent": parent,
            "object_kind_root": entity_type,
        }

    # Exact known names first; these are intentionally tiny and auditable.
    for kind, spec in type_taxonomy.items():
        for known in spec.get("known") or []:
            known_norm = canonicalize_entity_name(str(known))
            if known_norm and (
                name_match == known_norm or _norm_contains(name_match, known_norm)
            ):
                return _facet(str(kind), spec)

    # Extension and filename checks catch common document/code artifacts before
    # general synonym matching.
    if entity_type == "Document":
        if raw_lower.endswith((".pdf", ".doc", ".docx")) or _norm_contains(
            haystack, "report"
        ):
            spec = type_taxonomy.get("Report", {})
            return _facet("Report", spec)
        if raw_lower.endswith((".md", ".txt")) and _norm_contains(haystack, "tutorial"):
            spec = type_taxonomy.get("Tutorial", {})
            return _facet("Tutorial", spec)
    if entity_type == "Artifact":
        if raw_lower.endswith((".dll", ".so", ".jar")):
            spec = type_taxonomy.get("Library", {})
            return _facet("Library", spec)
        if raw_lower.endswith((".exe", ".app")):
            spec = type_taxonomy.get("Tool", {})
            return _facet("Tool", spec)

    # Synonym match over normalized name + optional context.
    for kind, spec in type_taxonomy.items():
        for term in spec.get("synonyms") or []:
            if _norm_contains(haystack, str(term)):
                return _facet(str(kind), spec)

    return {}


def resolve_domain_type(
    entity_name: str,
    entity_type: str = "",
    text_context: str = "",
) -> dict[str, str]:
    """Infer PRD/app-design role facets for product-spec corpora.

    Universal entity types stay intentionally broad. PRD documents need a
    second lens that says whether an entity behaves like a Feature, Module,
    DataObject, AIModel, Constraint, Risk, Milestone, etc. This deterministic
    facet gives Mission Control product-design semantics without asking Ghost B
    to emit extra fields or changing the global schema.
    """
    taxonomy = _load_domain_taxonomy()
    if not taxonomy:
        return {}

    raw_name = str(entity_name or "")
    name_norm = canonicalize_entity_name(raw_name).replace("_", " ")
    context_norm = normalize_entity_name(text_context)
    haystack = " ".join(part for part in (name_norm, context_norm) if part)
    if not haystack:
        return {}

    def _domain(kind: str, spec: dict) -> dict[str, str]:
        return {
            "domain_type": kind,
            "domain_type_parent": str(spec.get("parent") or "AppDesign"),
            "domain_type_root": str(spec.get("root") or "PRD"),
        }

    # Known names are higher precision than generic synonym words.
    for kind, spec in taxonomy.items():
        if not isinstance(spec, dict):
            continue
        for known in spec.get("known") or []:
            known_norm = canonicalize_entity_name(str(known)).replace("_", " ")
            if known_norm and (
                name_norm == known_norm or _norm_contains(name_norm, known_norm)
            ):
                return _domain(str(kind), spec)

    for kind, spec in taxonomy.items():
        if not isinstance(spec, dict):
            continue
        for term in spec.get("synonyms") or []:
            if _norm_contains(haystack, str(term)):
                return _domain(str(kind), spec)

    return {}


def resolve_ontology_metadata(
    entity_name: str,
    entity_type: str,
    text_context: str = "",
) -> dict[str, str]:
    """Resolve all deterministic ontology metadata stored on Entity nodes."""
    metadata = resolve_facets(entity_name, entity_type, text_context)
    metadata.update(resolve_domain_type(entity_name, entity_type, text_context))
    family = resolve_canonical_family(entity_name, text_context)
    if family:
        metadata["canonical_family"] = family
    metadata["ontology_version"] = ONTOLOGY_VERSION
    return metadata


def _slugify_type(entity_type: str) -> str:
    """Lowercase, alphanumerics + hyphens only. Empty → 'other'."""
    slug = re.sub(r"[^a-z0-9]+", "-", (entity_type or "").lower()).strip("-")
    return slug or SchemaContext.ENTITY_SENTINEL


def _slugify_name(canonical_name: str) -> str:
    """Normalized canonical name → URL-safe slug (spaces → hyphens, no punctuation)."""
    return canonicalize_entity_name(canonical_name).replace(" ", "-")


def entity_id_from_name(canonical_name: str, entity_type: str | None = None) -> str:
    """Deterministic canonical entity ID.

    Format: `entity:{name_slug}`.

    The `entity_type` argument is intentionally ignored and retained only for
    call-site compatibility. Type is now extraction evidence, stored as
    primary_entity_type / observed_entity_types on Entity and extracted_type on
    MENTIONS. This prevents Product:pvector, Method:pvector, and Concept:pvector
    from becoming separate graph nodes.
    """
    return f"{ENTITY_ID_PREFIX}:{_slugify_name(canonical_name)}"


def is_generic_entity_name(canonical_name: str) -> bool:
    normalized = canonicalize_entity_name(canonical_name)
    return normalized in GENERIC_ENTITY_TERMS


def _support_id_for_row(
    *,
    edge_key: str,
    corpus_id: str,
    doc_id: str,
    parent_id: str,
    chunk_id: str,
) -> str:
    raw = "\x1f".join([edge_key, corpus_id, doc_id, parent_id, chunk_id])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _relation_support_records(
    *,
    relation_rows: list[dict],
    corpus_id: str,
    doc_id: str,
    chunk_parent_ids: dict[str, str] | None,
) -> list[dict]:
    parent_lookup = chunk_parent_ids or {}
    records: list[dict] = []
    for row in relation_rows:
        chunk_id = str(row.get("chunk_id") or "")
        if not chunk_id:
            continue
        source_entity_id = str(row.get("subject_id") or "")
        target_entity_id = str(row.get("object_id") or "")
        predicate = str(row.get("predicate") or "")
        if not source_entity_id or not target_entity_id or not predicate:
            continue
        row_doc_id = str(row.get("doc_id") or doc_id)
        parent_id = str(row.get("parent_id") or parent_lookup.get(chunk_id) or "")
        edge_key = "|".join([source_entity_id, predicate, target_entity_id])
        records.append(
            {
                "support_id": _support_id_for_row(
                    edge_key=edge_key,
                    corpus_id=corpus_id,
                    doc_id=row_doc_id,
                    parent_id=parent_id,
                    chunk_id=chunk_id,
                ),
                "edge_key": edge_key,
                "source_entity_id": source_entity_id,
                "predicate": predicate,
                "target_entity_id": target_entity_id,
                "corpus_id": corpus_id,
                "doc_id": row_doc_id,
                "parent_id": parent_id,
                "chunk_id": chunk_id,
                "evidence_quote": row.get("evidence_phrase") or "",
                "confidence": float(row.get("confidence") or 0.0),
                "relation_family": row.get("relation_family") or "",
                "source_predicate": row.get("source_predicate") or predicate,
                "edge_state": row.get("edge_state") or "",
                "fallback": bool(row.get("fallback") or False),
                "fallback_family": row.get("fallback_family") or "",
                "candidate_predicates": list(row.get("candidate_predicates") or []),
                "candidate_scores": list(row.get("candidate_scores") or []),
                "candidate_score_sources": list(
                    row.get("candidate_score_sources") or []
                ),
                "promoted_by": row.get("promoted_by") or "",
                "related_to_query_weight": float(
                    row.get("related_to_query_weight") or 1.0
                ),
                "related_to_max_hops": int(row.get("related_to_max_hops") or 2),
                "relation_cue": row.get("relation_cue") or "",
                "validation_status": row.get("validation_status") or "",
                "extract_schema_version": row.get("schema_version")
                or "polymath.extract.v1",
                "promote_version": GRAPH_PROMOTE_VERSION,
            }
        )
    return records


async def _refresh_entity_aggregates(session, entity_ids: list[str]) -> None:
    unique_ids = [eid for eid in dict.fromkeys(entity_ids) if eid]
    for idx in range(0, len(unique_ids), GRAPH_ENTITY_AGGREGATE_BATCH_SIZE):
        batch = unique_ids[idx : idx + GRAPH_ENTITY_AGGREGATE_BATCH_SIZE]
        await session.run(
            """
            UNWIND $entity_ids AS entity_id
            MATCH (e:Entity {entity_id: entity_id})
            WITH e, toLower(coalesce(e.canonical_name, e.normalized_name, e.display_name, '')) AS entity_name
            OPTIONAL MATCH (c:Chunk)-[:MENTIONS]->(e)
            WITH e, entity_name,
                 count(DISTINCT c.chunk_id) AS mentions,
                 [cid IN collect(DISTINCT c.corpus_id) WHERE cid IS NOT NULL] AS source_corpora
            OPTIONAL MATCH (e)-[r:RELATES_TO]-()
            WITH e, entity_name, mentions, source_corpora, count(DISTINCT r) AS graph_degree,
                 entity_name IN $generic_terms AS is_generic
            SET e.source_corpora = source_corpora,
                e.corpus_count = size(source_corpora),
                e.mentions = mentions,
                e.graph_degree = graph_degree,
                e.generic_entity = coalesce(e.generic_entity, is_generic),
                e.ambiguous = coalesce(e.ambiguous, is_generic),
                e.needs_review = coalesce(e.needs_review, is_generic),
                e.graph_expansion_allowed = CASE
                    WHEN is_generic THEN false
                    ELSE coalesce(e.graph_expansion_allowed, true)
                END
            """,
            entity_ids=batch,
            generic_terms=list(GENERIC_ENTITY_TERMS),
        )


async def _resolve_entity_id_redirects(session, ids: list[str]) -> dict[str, str]:
    """Map merged-away entity ids to their live survivors before graph writes.

    Dedup apply leaves tombstones as edgeless redirect records:
    `tombstone:<old_id> -> merged_into=<survivor_id>`. Re-ingest must follow
    those redirects before any MERGE, otherwise it recreates the old entity id.
    """
    unique_ids = [eid for eid in dict.fromkeys(ids) if eid]
    if not unique_ids:
        return {}
    return await resolve_entity_ids(session, unique_ids)


def _row_batches(rows: list[Any], *, batch_size: int) -> list[list[Any]]:
    """Return stable, non-empty slices for transaction-bounded graph writes."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    return [rows[idx : idx + batch_size] for idx in range(0, len(rows), batch_size)]


async def _collect_distinct_entity_ids(
    session,
    *,
    corpus_id: str,
    doc_id: str | None = None,
) -> list[str]:
    """Stream affected entity IDs without a server-side unbounded ``collect``."""
    if doc_id is None:
        query = """
            MATCH (c:Chunk {corpus_id: $corpus_id})-[:MENTIONS]->(e:Entity)
            RETURN DISTINCT e.entity_id AS entity_id
        """
        params = {"corpus_id": corpus_id}
    else:
        query = """
            MATCH (c:Chunk {doc_id: $doc_id, corpus_id: $corpus_id})-[:MENTIONS]->(e:Entity)
            RETURN DISTINCT e.entity_id AS entity_id
        """
        params = {"doc_id": doc_id, "corpus_id": corpus_id}
    result = await session.run(query, **params)
    entity_ids: list[str] = []
    async for row in result:
        entity_id = str(row.get("entity_id") or "")
        if entity_id:
            entity_ids.append(entity_id)
    return entity_ids


async def _collect_chunk_rows(
    session,
    *,
    corpus_id: str,
    doc_id: str | None = None,
) -> list[dict[str, str]]:
    """Stream chunk/doc identities used by bounded provenance pruning."""
    if doc_id is None:
        query = """
            MATCH (c:Chunk {corpus_id: $corpus_id})
            RETURN DISTINCT c.chunk_id AS chunk_id, c.doc_id AS doc_id
        """
        params = {"corpus_id": corpus_id}
    else:
        query = """
            MATCH (c:Chunk {doc_id: $doc_id, corpus_id: $corpus_id})
            RETURN DISTINCT c.chunk_id AS chunk_id, c.doc_id AS doc_id
        """
        params = {"doc_id": doc_id, "corpus_id": corpus_id}
    result = await session.run(query, **params)
    rows: list[dict[str, str]] = []
    async for row in result:
        chunk_id = str(row.get("chunk_id") or "")
        row_doc_id = str(row.get("doc_id") or "")
        if chunk_id or row_doc_id:
            rows.append({"chunk_id": chunk_id, "doc_id": row_doc_id})
    return rows


async def _redirect_graph_write_rows(
    session,
    *,
    mention_rows: list[dict],
    relation_rows: list[dict],
    fact_rows: list[dict],
) -> dict[str, str]:
    ids: list[str] = []
    ids.extend(row.get("entity_id", "") for row in mention_rows)
    for row in relation_rows:
        ids.append(row.get("subject_id", ""))
        ids.append(row.get("object_id", ""))
    ids.extend(row.get("subject_entity_id", "") for row in fact_rows)

    redirects = await _resolve_entity_id_redirects(session, ids)
    if not redirects:
        return {}

    for row in mention_rows:
        original = row.get("entity_id")
        redirected = redirects.get(original)
        if redirected:
            row["resolved_from_entity_id"] = original
            row["entity_id"] = redirected
    for row in relation_rows:
        original_subject = row.get("subject_id")
        original_object = row.get("object_id")
        row["subject_id"] = redirects.get(original_subject, original_subject)
        row["object_id"] = redirects.get(original_object, original_object)
    for row in fact_rows:
        original = row.get("subject_entity_id")
        row["subject_entity_id"] = redirects.get(original, original)

    logger.info("Neo4j tombstone redirects applied before write: %d", len(redirects))
    return redirects


def fact_id_from_parts(
    *,
    doc_id: str,
    chunk_id: str,
    subject: str,
    property_name: str,
    value: str,
) -> str:
    """Deterministic fact ID scoped to source chunk and fact payload."""
    raw = "\x1f".join(
        [
            str(doc_id or ""),
            str(chunk_id or ""),
            canonicalize_entity_name(subject),
            str(property_name or "").strip().lower(),
            str(value or "").strip().lower(),
        ]
    )
    return f"fact:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


async def _upsert_document(
    driver: AsyncDriver,
    doc_id: str,
    corpus_id: str,
    user_id: str | None,
    file_id: str | None,
    *,
    filename: str | None = None,
    chunk_count: int = 0,
    parent_count: int = 0,
    source_path: str | None = None,
    source_tier: str | None = None,
    schema_lens_id: str | None = None,
    ghost_b_success_rate: float | None = None,
    ghost_b_extracted: int | None = None,
    ghost_b_total: int | None = None,
    dominant_family: str | None = None,
    dominant_entity_type: str | None = None,
) -> None:
    """Create or update a rich :Document anchor.

    Every Document is a cluster anchor by definition — `is_cluster_anchor=true`
    is set unconditionally so the Brain View Cypher (`WHERE d.is_cluster_anchor
    = true`) finds it without a separate backfill. The anchor stores enough
    metadata (filename, chunk_count, ghost_b health, dominant_family) for the
    front-end to render book cards without a MongoDB round-trip.

    Pt 6 scaling fix: dominant_family + dominant_entity_type are pre-computed
    at ingest and mirrored to the Document node. The Brain View Cypher reads
    these properties directly instead of an OPTIONAL MATCH traversal across
    every chunk×entity per anchor — drops a 100M-edge-walk query to a single
    indexed node read at 2000+ books.

    Optional kwargs use COALESCE on update so a partial caller (e.g. fresh
    ingest before ghost_b_metrics is computed) does not nuke values written
    by a later anchor-metrics update.
    """
    async with driver.session() as session:
        await session.run(
            """
            MERGE (d:Document {corpus_id: $corpus_id, doc_id: $doc_id})
            ON CREATE SET d.ingested_at = datetime()
            SET d.user_id = $user_id,
                d.file_id = $file_id,
                d.is_cluster_anchor = true,
                d.kind = 'book',
                d.node_type = 'Document',
                d.updated_at = datetime(),
                d.filename = coalesce($filename, d.filename),
                d.chunk_count = $chunk_count,
                d.parent_count = $parent_count,
                d.source_path = coalesce($source_path, d.source_path),
                d.source_tier = coalesce($source_tier, d.source_tier),
                d.schema_lens_id = coalesce($schema_lens_id, d.schema_lens_id),
                d.ghost_b_success_rate = coalesce($ghost_b_success_rate, d.ghost_b_success_rate),
                d.ghost_b_extracted = coalesce($ghost_b_extracted, d.ghost_b_extracted),
                d.ghost_b_total = coalesce($ghost_b_total, d.ghost_b_total),
                d.dominant_family = coalesce($dominant_family, d.dominant_family),
                d.dominant_entity_type = coalesce($dominant_entity_type, d.dominant_entity_type)
            """,
            doc_id=doc_id,
            corpus_id=corpus_id,
            user_id=user_id,
            file_id=file_id,
            filename=filename,
            chunk_count=int(chunk_count or 0),
            parent_count=int(parent_count or 0),
            source_path=source_path,
            source_tier=source_tier,
            schema_lens_id=schema_lens_id,
            ghost_b_success_rate=ghost_b_success_rate,
            ghost_b_extracted=ghost_b_extracted,
            ghost_b_total=ghost_b_total,
            dominant_family=dominant_family,
            dominant_entity_type=dominant_entity_type,
        )


def summarize_dominant_facets(
    extraction_results: list[ExtractionResult],
) -> tuple[str | None, str | None]:
    """Pt 6 scaling fix: compute dominant_canonical_family + dominant_entity_type
    once at ingest from the in-memory ExtractionResult list — same logic the
    Brain View Cypher used to run as an OPTIONAL MATCH per query.

    Strategy: tally every entity mention from every chunk; return the most
    frequent canonical_family + the most frequent primary_entity_type.
    Returns `(None, None)` when no entities were extracted (e.g. empty doc).

    The returned values feed `_upsert_document(dominant_family=...,
    dominant_entity_type=...)` so the Brain View Cypher can read the
    properties directly off the Document node instead of walking the
    chunk→entity graph for every anchor on every query.
    """
    from collections import Counter

    family_counter: Counter[str] = Counter()
    type_counter: Counter[str] = Counter()
    for result in extraction_results:
        # Pt 10b — feed chunk text as `text_context` so taxonomy synonym
        # matching can fire. Pre-fix this defaulted to "" and ~99% of
        # entities ended up with empty canonical_family.
        result_text = getattr(result, "text", "") or ""
        for entity in result.entities:
            if is_junk_extracted_entity(entity.canonical_name, entity.surface_form):
                continue
            canonical = canonicalize_entity_name(entity.canonical_name)
            if not canonical:
                continue
            primary_type = resolve_primary_entity_type(canonical, [entity.entity_type])
            ontology = resolve_ontology_metadata(canonical, primary_type, result_text)
            family = ontology.get("canonical_family")
            if family:
                family_counter[family] += 1
            if primary_type:
                type_counter[primary_type] += 1

    dominant_family = family_counter.most_common(1)[0][0] if family_counter else None
    dominant_entity_type = type_counter.most_common(1)[0][0] if type_counter else None
    return dominant_family, dominant_entity_type


async def update_document_anchor_metrics(
    driver: AsyncDriver,
    doc_id: str,
    corpus_id: str,
    *,
    chunk_count: int | None = None,
    parent_count: int | None = None,
    ghost_b_success_rate: float | None = None,
    ghost_b_extracted: int | None = None,
    ghost_b_total: int | None = None,
    schema_lens_id: str | None = None,
    dominant_family: str | None = None,
    dominant_entity_type: str | None = None,
) -> None:
    """Late-bound update of metrics on an existing :Document anchor.

    Called after Ghost B finishes and the worker has computed success rate,
    so the Brain View can render `success_rate` badges without a MongoDB
    lookup. No-op if the Document node does not exist yet (a fresh upsert
    would have created it via `_upsert_document`).

    Pt 6: also writes pre-computed dominant_family + dominant_entity_type
    so the Brain View Cypher can skip the chunk→entity OPTIONAL MATCH.
    """
    async with driver.session() as session:
        await session.run(
            """
            MATCH (d:Document {doc_id: $doc_id, corpus_id: $corpus_id})
            SET d.updated_at = datetime(),
                d.chunk_count = coalesce($chunk_count, d.chunk_count),
                d.parent_count = coalesce($parent_count, d.parent_count),
                d.ghost_b_success_rate = coalesce($ghost_b_success_rate, d.ghost_b_success_rate),
                d.ghost_b_extracted = coalesce($ghost_b_extracted, d.ghost_b_extracted),
                d.ghost_b_total = coalesce($ghost_b_total, d.ghost_b_total),
                d.schema_lens_id = coalesce($schema_lens_id, d.schema_lens_id),
                d.dominant_family = coalesce($dominant_family, d.dominant_family),
                d.dominant_entity_type = coalesce($dominant_entity_type, d.dominant_entity_type)
            """,
            doc_id=doc_id,
            corpus_id=corpus_id,
            chunk_count=int(chunk_count) if chunk_count is not None else None,
            parent_count=int(parent_count) if parent_count is not None else None,
            ghost_b_success_rate=(
                float(ghost_b_success_rate)
                if ghost_b_success_rate is not None
                else None
            ),
            ghost_b_extracted=(
                int(ghost_b_extracted) if ghost_b_extracted is not None else None
            ),
            ghost_b_total=(int(ghost_b_total) if ghost_b_total is not None else None),
            schema_lens_id=schema_lens_id,
            dominant_family=dominant_family,
            dominant_entity_type=dominant_entity_type,
        )


async def _upsert_chunk(
    driver: AsyncDriver,
    chunk_id: str,
    doc_id: str,
    corpus_id: str,
) -> None:
    async with driver.session() as session:
        await session.run(
            """
            MERGE (c:Chunk {corpus_id: $corpus_id, chunk_id: $chunk_id})
            SET c.doc_id = $doc_id
            WITH c
            MATCH (d:Document {doc_id: $doc_id, corpus_id: $corpus_id})
            MERGE (d)-[:HAS_CHUNK]->(c)
            """,
            chunk_id=chunk_id,
            doc_id=doc_id,
            corpus_id=corpus_id,
        )


async def _upsert_entity_and_mention(
    driver: AsyncDriver,
    entity: EntityItem,
    chunk_id: str,
    corpus_id: str,
    text_context: str = "",
) -> None:
    """Single-entity upsert path. Pt 10b — accepts `text_context` so taxonomy
    synonym matching can fire. Callers that have the chunk text should pass
    it; default empty preserves pre-fix behavior for back-compat (this
    function is currently unreferenced in the codebase but kept for the
    live-API/legacy path described in earlier comments).
    """
    if is_junk_extracted_entity(entity.canonical_name, entity.surface_form):
        return
    canonical = canonicalize_entity_name(entity.canonical_name)
    primary_type = resolve_primary_entity_type(canonical, [entity.entity_type])
    eid = entity_id_from_name(canonical, primary_type)
    ontology = resolve_ontology_metadata(canonical, primary_type, text_context)
    async with driver.session() as session:
        redirects = await _resolve_entity_id_redirects(session, [eid])
        resolved_from_entity_id = eid if eid in redirects else None
        eid = redirects.get(eid, eid)
        await session.run(
            """
            MERGE (e:Entity {entity_id: $entity_id})
            ON CREATE SET e.first_seen = timestamp()
            SET e.normalized_name = CASE
                    WHEN $resolved_from_entity_id IS NULL THEN $canonical_name
                    ELSE coalesce(e.normalized_name, $canonical_name)
                END,
                e.canonical_name = CASE
                    WHEN $resolved_from_entity_id IS NULL THEN $canonical_name
                    ELSE coalesce(e.canonical_name, $canonical_name)
                END,
                e.display_name = CASE
                    WHEN $resolved_from_entity_id IS NULL THEN $display_name
                    ELSE coalesce(e.display_name, $display_name)
                END,
                e.primary_entity_type = CASE
                    WHEN $resolved_from_entity_id IS NULL THEN $primary_entity_type
                    ELSE coalesce(e.primary_entity_type, $primary_entity_type)
                END,
                e.entity_type = CASE
                    WHEN $resolved_from_entity_id IS NULL THEN $primary_entity_type
                    ELSE coalesce(e.entity_type, $primary_entity_type)
                END,
                e.confidence = CASE
                    WHEN e.confidence IS NULL OR $confidence > e.confidence THEN $confidence
                    ELSE e.confidence
                END,
                e.object_kind = CASE
                    WHEN $resolved_from_entity_id IS NULL THEN $object_kind
                    ELSE coalesce(e.object_kind, $object_kind)
                END,
                e.object_kind_parent = CASE
                    WHEN $resolved_from_entity_id IS NULL THEN $object_kind_parent
                    ELSE coalesce(e.object_kind_parent, $object_kind_parent)
                END,
                e.object_kind_root = CASE
                    WHEN $resolved_from_entity_id IS NULL THEN $object_kind_root
                    ELSE coalesce(e.object_kind_root, $object_kind_root)
                END,
                e.domain_type = CASE
                    WHEN $resolved_from_entity_id IS NULL THEN $domain_type
                    ELSE coalesce(e.domain_type, $domain_type)
                END,
                e.domain_type_parent = CASE
                    WHEN $resolved_from_entity_id IS NULL THEN $domain_type_parent
                    ELSE coalesce(e.domain_type_parent, $domain_type_parent)
                END,
                e.domain_type_root = CASE
                    WHEN $resolved_from_entity_id IS NULL THEN $domain_type_root
                    ELSE coalesce(e.domain_type_root, $domain_type_root)
                END,
                e.canonical_family = CASE
                    WHEN $resolved_from_entity_id IS NULL THEN $canonical_family
                    ELSE coalesce(e.canonical_family, $canonical_family)
                END,
                e.ontology_version = CASE
                    WHEN $resolved_from_entity_id IS NULL THEN $ontology_version
                    ELSE coalesce(e.ontology_version, $ontology_version)
                END,
                e.query_aliases = CASE
                    WHEN $query_aliases IS NULL OR size($query_aliases) = 0 THEN coalesce(e.query_aliases, [])
                    ELSE [a IN $query_aliases WHERE NOT a IN coalesce(e.query_aliases, [])] + coalesce(e.query_aliases, [])
                END,
                e.definitional_phrase = CASE
                    WHEN $definitional_phrase IS NULL OR $definitional_phrase = '' THEN coalesce(e.definitional_phrase, '')
                    WHEN coalesce(e.definitional_phrase, '') = '' THEN $definitional_phrase
                    ELSE e.definitional_phrase
                END
            WITH e
            SET e.observed_entity_types = CASE
                WHEN e.observed_entity_types IS NULL THEN [$extracted_type]
                WHEN $extracted_type IN e.observed_entity_types THEN e.observed_entity_types
                ELSE e.observed_entity_types + [$extracted_type]
            END
            WITH e
            MATCH (c:Chunk {chunk_id: $chunk_id, corpus_id: $corpus_id})
            MERGE (c)-[m:MENTIONS]->(e)
            SET m.confidence = CASE
                    WHEN m.confidence IS NULL OR $confidence > m.confidence THEN $confidence
                    ELSE m.confidence
                END,
                m.extracted_type = $extracted_type,
                m.surface_form = $surface_form,
                m.extractor = 'ghost_b',
                m.ontology_version = $ontology_version
            SET m.extracted_types = CASE
                WHEN m.extracted_types IS NULL THEN [$extracted_type]
                WHEN $extracted_type IN m.extracted_types THEN m.extracted_types
                ELSE m.extracted_types + [$extracted_type]
            END
            """,
            entity_id=eid,
            resolved_from_entity_id=resolved_from_entity_id,
            canonical_name=canonical,
            display_name=entity.surface_form or entity.canonical_name,
            surface_form=entity.surface_form or entity.canonical_name,
            primary_entity_type=primary_type,
            extracted_type=entity.entity_type,
            confidence=entity.confidence,
            object_kind=ontology.get("object_kind"),
            object_kind_parent=ontology.get("object_kind_parent"),
            object_kind_root=ontology.get("object_kind_root"),
            domain_type=ontology.get("domain_type"),
            domain_type_parent=ontology.get("domain_type_parent"),
            domain_type_root=ontology.get("domain_type_root"),
            canonical_family=ontology.get("canonical_family"),
            ontology_version=ontology.get("ontology_version"),
            # Pt 10c — query-facing fields. Both default-safe.
            query_aliases=list(getattr(entity, "query_aliases", []) or []),
            definitional_phrase=(getattr(entity, "definitional_phrase", "") or "")[
                :200
            ],
            chunk_id=chunk_id,
            corpus_id=corpus_id,
        )


async def _upsert_relation(
    driver: AsyncDriver,
    relation: RelationItem,
    name_to_type: dict[str, str],
) -> None:
    """Upsert RELATES_TO edge between two entities.

    Phase 14.3: looks up subject/object entity_type from `name_to_type` (built
    from this document's extracted entities). When a relation references a name
    the LLM didn't extract as an entity in any chunk of this document, we fall
    back to the ENTITY_SENTINEL ('other') namespace so the edge is preserved.
    """
    if relation.object_kind != "entity":
        return
    subject_type = name_to_type.get(
        canonicalize_entity_name(relation.subject), SchemaContext.ENTITY_SENTINEL
    )
    object_type = name_to_type.get(
        canonicalize_entity_name(relation.object), SchemaContext.ENTITY_SENTINEL
    )
    subject_id = entity_id_from_name(relation.subject, subject_type)
    object_id = entity_id_from_name(relation.object, object_type)
    edge_mitigation = build_relation_edge_mitigation(
        extracted_predicate=relation.predicate,
        refined_predicate=relation.predicate,
        source_predicate=relation.source_predicate or relation.predicate,
        confidence=relation.confidence,
        evidence_phrase=relation.evidence_phrase,
        relation_cue=relation.relation_cue,
        predicate_refined=False,
    )
    relation_family = edge_mitigation.relation_family
    edge_strength = relation_edge_strength(
        relation.predicate,
        relation.confidence,
        relation.validation_status,
        predicate_refined=False,
    )
    async with driver.session() as session:
        redirects = await _resolve_entity_id_redirects(session, [subject_id, object_id])
        subject_id = redirects.get(subject_id, subject_id)
        object_id = redirects.get(object_id, object_id)
        await session.run(
            """
            MATCH (s:Entity {entity_id: $subject_id})
            MATCH (o:Entity {entity_id: $object_id})
            MERGE (s)-[r:RELATES_TO {predicate: $predicate}]->(o)
            SET r.confidence = $confidence,
                r.relation_family = $relation_family,
                r.edge_strength = $edge_strength,
                r.eligible_for_synthesis = $eligible_for_synthesis,
                r.edge_state = $edge_state,
                r.fallback = $fallback,
                r.fallback_family = $fallback_family,
                r.candidate_predicates = $candidate_predicates,
                r.candidate_scores = $candidate_scores,
                r.candidate_score_sources = $candidate_score_sources,
                r.fallback_evidence_phrase = $fallback_evidence_phrase,
                r.related_to_query_weight = $related_to_query_weight,
                r.related_to_max_hops = $related_to_max_hops
            """,
            subject_id=subject_id,
            object_id=object_id,
            predicate=relation.predicate,
            relation_family=relation_family,
            edge_strength=edge_strength,
            eligible_for_synthesis=relation_eligible_for_synthesis(
                relation.predicate, relation.confidence, relation.validation_status
            ),
            edge_state=edge_mitigation.edge_state,
            fallback=edge_mitigation.fallback,
            fallback_family=edge_mitigation.fallback_family,
            candidate_predicates=edge_mitigation.candidate_predicates,
            candidate_scores=edge_mitigation.candidate_scores,
            candidate_score_sources=edge_mitigation.candidate_score_sources,
            fallback_evidence_phrase=edge_mitigation.fallback_evidence_phrase,
            related_to_query_weight=edge_mitigation.related_to_query_weight,
            related_to_max_hops=edge_mitigation.related_to_max_hops,
            confidence=relation.confidence,
        )


async def _delete_orphan_entities(session) -> None:
    """Remove Entity nodes without chunk evidence in bounded transactions."""
    while True:
        result = await session.run(
            """
        MATCH (e:Entity)
        WHERE coalesce(e.tombstone, false) = false
          AND NOT EXISTS { MATCH (:Chunk)-[:MENTIONS]->(e) }
            WITH e LIMIT $batch_size
        DETACH DELETE e
            RETURN count(e) AS deleted
            """,
            batch_size=GRAPH_DELETE_BATCH_SIZE,
        )
        row = await result.single()
        if not row or int(row.get("deleted") or 0) == 0:
            break


async def _prune_relates_to_for_document(
    session,
    *,
    corpus_id: str,
    doc_id: str,
) -> None:
    """Remove one document's support from RELATES_TO provenance arrays.

    Entity nodes are global and RELATES_TO edges can be supported by multiple
    documents. Deleting a document should therefore prune only that document's
    evidence, then delete the relationship when no corpus still supports it.
    """
    chunk_rows = await _collect_chunk_rows(
        session,
        corpus_id=corpus_id,
        doc_id=doc_id,
    )
    chunk_ids_all = [row["chunk_id"] for row in chunk_rows if row["chunk_id"]]
    corpus_prefix = f"{corpus_id}{IDENTITY_KEY_SEPARATOR}"
    doc_key = corpus_content_key(corpus_id, doc_id)
    ambiguity = await session.run(
        """
        MATCH ()-[r:RELATES_TO]->()
        WHERE $corpus_id IN coalesce(r.corpus_ids, [])
          AND (
              any(chunk_id IN coalesce(r.evidence_chunk_ids, []) WHERE chunk_id IN $chunk_ids)
              OR $doc_id IN coalesce(r.evidence_doc_ids, [])
              OR r.latest_doc_id = $doc_id
          )
          AND none(key IN coalesce(r.evidence_chunk_keys, []) WHERE key IN $chunk_keys)
          AND NOT $doc_key IN coalesce(r.evidence_doc_keys, [])
          AND coalesce(r.latest_doc_key, '') <> $doc_key
          AND (
              any(cid IN coalesce(r.corpus_ids, []) WHERE cid <> $corpus_id)
              OR any(key IN coalesce(r.evidence_chunk_keys, []) WHERE NOT key STARTS WITH $corpus_prefix)
              OR any(key IN coalesce(r.evidence_doc_keys, []) WHERE NOT key STARTS WITH $corpus_prefix)
          )
        RETURN count(r) AS ambiguous
        """,
        corpus_id=corpus_id,
        corpus_prefix=corpus_prefix,
        doc_id=doc_id,
        doc_key=doc_key,
        chunk_ids=chunk_ids_all,
        chunk_keys=[corpus_content_key(corpus_id, value) for value in chunk_ids_all],
    )
    ambiguity_row = await ambiguity.single()
    ambiguous = int(ambiguity_row.get("ambiguous") or 0) if ambiguity_row else 0
    if ambiguous:
        raise RuntimeError(
            "ambiguous_legacy_relation_provenance: "
            f"document {corpus_id}/{doc_id} has {ambiguous} shared RELATES_TO edges "
            "without corpus-qualified evidence"
        )
    batches = _row_batches(
        chunk_rows,
        batch_size=GRAPH_RELATION_PRUNE_BATCH_SIZE,
    ) or [[]]
    for batch in batches:
        chunk_ids = [row["chunk_id"] for row in batch if row["chunk_id"]]
        chunk_keys = [corpus_content_key(corpus_id, value) for value in chunk_ids]
        while True:
            result = await session.run(
                """
                MATCH ()-[r:RELATES_TO]->()
                WHERE any(key IN coalesce(r.evidence_chunk_keys, []) WHERE key IN $chunk_keys)
                   OR $doc_key IN coalesce(r.evidence_doc_keys, [])
                   OR r.latest_doc_key = $doc_key
                   OR (
                       $corpus_id IN coalesce(r.corpus_ids, [])
                       AND size(coalesce(r.evidence_chunk_keys, [])) = 0
                       AND size(coalesce(r.evidence_doc_keys, [])) = 0
                       AND none(cid IN coalesce(r.corpus_ids, []) WHERE cid <> $corpus_id)
                       AND any(chunk_id IN coalesce(r.evidence_chunk_ids, []) WHERE chunk_id IN $chunk_ids)
                   )
                   OR (
                       $corpus_id IN coalesce(r.corpus_ids, [])
                       AND size(coalesce(r.evidence_chunk_keys, [])) = 0
                       AND size(coalesce(r.evidence_doc_keys, [])) = 0
                       AND none(cid IN coalesce(r.corpus_ids, []) WHERE cid <> $corpus_id)
                       AND ($doc_id IN coalesce(r.evidence_doc_ids, []) OR r.latest_doc_id = $doc_id)
                   )
                WITH r LIMIT $batch_size
                WITH r,
                     any(key IN coalesce(r.evidence_chunk_keys, []) WHERE key STARTS WITH $corpus_prefix) AS had_scoped_chunk_keys,
                     any(key IN coalesce(r.evidence_doc_keys, []) WHERE key STARTS WITH $corpus_prefix) AS had_scoped_doc_keys,
                     any(key IN coalesce(r.support_confidence_chunk_keys, []) WHERE key STARTS WITH $corpus_prefix) AS had_scoped_support_keys,
                     [key IN coalesce(r.evidence_chunk_keys, []) WHERE NOT key IN $chunk_keys] AS remaining_chunk_keys,
                     [key IN coalesce(r.evidence_doc_keys, []) WHERE key <> $doc_key] AS remaining_doc_keys,
                     coalesce(r.support_confidence_chunk_keys, []) AS support_keys,
                     coalesce(r.support_confidence_values_v2, []) AS support_values_v2
                SET r.evidence_chunk_keys = remaining_chunk_keys,
                    r.evidence_doc_keys = remaining_doc_keys,
                    r.latest_doc_key = CASE WHEN r.latest_doc_key = $doc_key THEN NULL ELSE r.latest_doc_key END,
                    r.evidence_chunk_ids = [
                        chunk_id IN coalesce(r.evidence_chunk_ids, [])
                        WHERE NOT chunk_id IN $chunk_ids
                           OR (had_scoped_chunk_keys AND any(key IN remaining_chunk_keys WHERE split(key, $separator)[1] = chunk_id))
                    ],
                    r.evidence_doc_ids = [
                        rel_doc_id IN coalesce(r.evidence_doc_ids, [])
                        WHERE rel_doc_id <> $doc_id
                           OR (had_scoped_doc_keys AND any(key IN remaining_doc_keys WHERE split(key, $separator)[1] = rel_doc_id))
                    ],
                    r.latest_doc_id = CASE
                        WHEN r.latest_doc_id = $doc_id
                         AND NOT any(key IN remaining_doc_keys WHERE split(key, $separator)[1] = $doc_id)
                        THEN NULL ELSE r.latest_doc_id
                    END,
                    r.support_confidence_chunk_keys = [key IN support_keys WHERE NOT key IN $chunk_keys],
                    r.support_confidence_values_v2 = CASE
                        WHEN size(support_keys) = 0 THEN []
                        ELSE [
                            idx IN range(0, size(support_keys) - 1)
                            WHERE idx < size(support_values_v2) AND NOT support_keys[idx] IN $chunk_keys
                            | support_values_v2[idx]
                        ]
                    END,
                    r.support_confidence_chunk_ids = [
                        support_id IN coalesce(r.support_confidence_chunk_ids, [])
                        WHERE NOT support_id IN $chunk_ids OR had_scoped_support_keys
                    ]
                WITH r
                OPTIONAL MATCH (remaining:Chunk {corpus_id: $corpus_id})
                WHERE remaining.chunk_id IN coalesce(r.evidence_chunk_ids, [])
                  AND remaining.doc_id <> $doc_id
                WITH r, count(remaining) AS remaining_corpus_support
                SET r.corpus_ids = CASE
                    WHEN remaining_corpus_support = 0
                     AND none(key IN coalesce(r.evidence_chunk_keys, []) WHERE key STARTS WITH $corpus_prefix)
                    THEN [cid IN coalesce(r.corpus_ids, []) WHERE cid <> $corpus_id]
                    ELSE coalesce(r.corpus_ids, [])
                END
                SET r.support_count = CASE
                        WHEN size(coalesce(r.evidence_chunk_keys, [])) > size(coalesce(r.evidence_chunk_ids, []))
                        THEN size(coalesce(r.evidence_chunk_keys, []))
                        ELSE size(coalesce(r.evidence_chunk_ids, []))
                    END,
                    r.avg_confidence = CASE
                        WHEN size(coalesce(r.support_confidence_values_v2, [])) > 0
                        THEN reduce(total = 0.0, conf IN r.support_confidence_values_v2 | total + toFloat(conf))
                             / size(r.support_confidence_values_v2)
                        WHEN size(coalesce(r.support_confidence_values, [])) > 0
                        THEN reduce(total = 0.0, conf IN r.support_confidence_values | total + toFloat(conf))
                             / size(r.support_confidence_values)
                        ELSE toFloat(coalesce(r.confidence, 0.0))
                    END
                WITH r, size(coalesce(r.corpus_ids, [])) = 0 AS delete_relation
                FOREACH (_ IN CASE WHEN delete_relation THEN [1] ELSE [] END | DELETE r)
                RETURN count(*) AS updated
                """,
                corpus_id=corpus_id,
                corpus_prefix=corpus_prefix,
                doc_id=doc_id,
                doc_key=doc_key,
                chunk_ids=chunk_ids,
                chunk_keys=chunk_keys,
                separator=IDENTITY_KEY_SEPARATOR,
                batch_size=GRAPH_RELATION_PRUNE_BATCH_SIZE,
            )
            row = await result.single()
            if not row or int(row.get("updated") or 0) == 0:
                break


async def _prune_relates_to_for_corpus(
    session,
    *,
    corpus_id: str,
) -> None:
    """Remove an entire corpus from RELATES_TO provenance arrays."""
    chunk_rows = await _collect_chunk_rows(session, corpus_id=corpus_id)
    batches = _row_batches(
        chunk_rows,
        batch_size=GRAPH_RELATION_PRUNE_BATCH_SIZE,
    ) or [[]]
    corpus_prefix = f"{corpus_id}{IDENTITY_KEY_SEPARATOR}"
    ambiguity = await session.run(
        """
        MATCH ()-[r:RELATES_TO]->()
        WHERE $corpus_id IN coalesce(r.corpus_ids, [])
          AND none(key IN coalesce(r.evidence_chunk_keys, []) WHERE key STARTS WITH $corpus_prefix)
          AND none(key IN coalesce(r.evidence_doc_keys, []) WHERE key STARTS WITH $corpus_prefix)
          AND any(cid IN coalesce(r.corpus_ids, []) WHERE cid <> $corpus_id)
        RETURN count(r) AS ambiguous
        """,
        corpus_id=corpus_id,
        corpus_prefix=corpus_prefix,
    )
    ambiguity_row = await ambiguity.single()
    ambiguous = int(ambiguity_row.get("ambiguous") or 0) if ambiguity_row else 0
    if ambiguous:
        raise RuntimeError(
            "ambiguous_legacy_relation_provenance: "
            f"corpus {corpus_id} has {ambiguous} shared RELATES_TO edges "
            "without corpus-qualified evidence"
        )
    for batch in batches:
        chunk_ids = [row["chunk_id"] for row in batch if row["chunk_id"]]
        doc_ids = list(dict.fromkeys(row["doc_id"] for row in batch if row["doc_id"]))
        chunk_keys = [corpus_content_key(corpus_id, value) for value in chunk_ids]
        doc_keys = [corpus_content_key(corpus_id, value) for value in doc_ids]
        while True:
            result = await session.run(
                """
                MATCH ()-[r:RELATES_TO]->()
                WHERE $corpus_id IN coalesce(r.corpus_ids, [])
                   OR any(key IN coalesce(r.evidence_chunk_keys, []) WHERE key STARTS WITH $corpus_prefix)
                   OR any(key IN coalesce(r.evidence_doc_keys, []) WHERE key STARTS WITH $corpus_prefix)
                   OR (
                       $corpus_id IN coalesce(r.corpus_ids, [])
                       AND size(coalesce(r.evidence_chunk_keys, [])) = 0
                       AND size(coalesce(r.evidence_doc_keys, [])) = 0
                       AND none(cid IN coalesce(r.corpus_ids, []) WHERE cid <> $corpus_id)
                       AND any(chunk_id IN coalesce(r.evidence_chunk_ids, []) WHERE chunk_id IN $chunk_ids)
                   )
                   OR (
                       $corpus_id IN coalesce(r.corpus_ids, [])
                       AND size(coalesce(r.evidence_chunk_keys, [])) = 0
                       AND size(coalesce(r.evidence_doc_keys, [])) = 0
                       AND none(cid IN coalesce(r.corpus_ids, []) WHERE cid <> $corpus_id)
                       AND any(rel_doc_id IN coalesce(r.evidence_doc_ids, []) WHERE rel_doc_id IN $doc_ids)
                   )
                WITH r LIMIT $batch_size
                WITH r,
                     any(key IN coalesce(r.evidence_chunk_keys, []) WHERE key STARTS WITH $corpus_prefix) AS had_scoped_chunk_keys,
                     any(key IN coalesce(r.evidence_doc_keys, []) WHERE key STARTS WITH $corpus_prefix) AS had_scoped_doc_keys,
                     any(key IN coalesce(r.support_confidence_chunk_keys, []) WHERE key STARTS WITH $corpus_prefix) AS had_scoped_support_keys,
                     [key IN coalesce(r.evidence_chunk_keys, []) WHERE NOT key STARTS WITH $corpus_prefix] AS remaining_chunk_keys,
                     [key IN coalesce(r.evidence_doc_keys, []) WHERE NOT key STARTS WITH $corpus_prefix] AS remaining_doc_keys,
                     coalesce(r.support_confidence_chunk_keys, []) AS support_keys,
                     coalesce(r.support_confidence_values_v2, []) AS support_values_v2
                SET r.corpus_ids = [cid IN coalesce(r.corpus_ids, []) WHERE cid <> $corpus_id],
                    r.evidence_chunk_keys = remaining_chunk_keys,
                    r.evidence_doc_keys = remaining_doc_keys,
                    r.latest_doc_key = CASE WHEN r.latest_doc_key STARTS WITH $corpus_prefix THEN NULL ELSE r.latest_doc_key END,
                    r.evidence_chunk_ids = [
                        chunk_id IN coalesce(r.evidence_chunk_ids, [])
                        WHERE NOT chunk_id IN $chunk_ids
                           OR (had_scoped_chunk_keys AND any(key IN remaining_chunk_keys WHERE split(key, $separator)[1] = chunk_id))
                    ],
                    r.evidence_doc_ids = [
                        rel_doc_id IN coalesce(r.evidence_doc_ids, [])
                        WHERE NOT rel_doc_id IN $doc_ids
                           OR (had_scoped_doc_keys AND any(key IN remaining_doc_keys WHERE split(key, $separator)[1] = rel_doc_id))
                    ],
                    r.latest_doc_id = CASE
                        WHEN r.latest_doc_id IN $doc_ids
                         AND NOT any(key IN remaining_doc_keys WHERE split(key, $separator)[1] = r.latest_doc_id)
                        THEN NULL ELSE r.latest_doc_id
                    END,
                    r.support_confidence_chunk_keys = [key IN support_keys WHERE NOT key STARTS WITH $corpus_prefix],
                    r.support_confidence_values_v2 = CASE
                        WHEN size(support_keys) = 0 THEN []
                        ELSE [
                            idx IN range(0, size(support_keys) - 1)
                            WHERE idx < size(support_values_v2) AND NOT support_keys[idx] STARTS WITH $corpus_prefix
                            | support_values_v2[idx]
                        ]
                    END,
                    r.support_confidence_chunk_ids = [
                        support_id IN coalesce(r.support_confidence_chunk_ids, [])
                        WHERE NOT support_id IN $chunk_ids OR had_scoped_support_keys
                    ]
                SET r.support_count = CASE
                        WHEN size(coalesce(r.evidence_chunk_keys, [])) > size(coalesce(r.evidence_chunk_ids, []))
                        THEN size(coalesce(r.evidence_chunk_keys, []))
                        ELSE size(coalesce(r.evidence_chunk_ids, []))
                    END,
                    r.avg_confidence = CASE
                        WHEN size(coalesce(r.support_confidence_values_v2, [])) > 0
                        THEN reduce(total = 0.0, conf IN r.support_confidence_values_v2 | total + toFloat(conf))
                             / size(r.support_confidence_values_v2)
                        WHEN size(coalesce(r.support_confidence_values, [])) > 0
                        THEN reduce(total = 0.0, conf IN r.support_confidence_values | total + toFloat(conf))
                             / size(r.support_confidence_values)
                        ELSE toFloat(coalesce(r.confidence, 0.0))
                    END
                WITH r, size(coalesce(r.corpus_ids, [])) = 0 AS delete_relation
                FOREACH (_ IN CASE WHEN delete_relation THEN [1] ELSE [] END | DELETE r)
                RETURN count(*) AS updated
                """,
                corpus_id=corpus_id,
                corpus_prefix=corpus_prefix,
                chunk_ids=chunk_ids,
                chunk_keys=chunk_keys,
                doc_ids=doc_ids,
                doc_keys=doc_keys,
                separator=IDENTITY_KEY_SEPARATOR,
                batch_size=GRAPH_RELATION_PRUNE_BATCH_SIZE,
            )
            row = await result.single()
            if not row or int(row.get("updated") or 0) == 0:
                break


async def delete_document_graph(
    driver: AsyncDriver,
    *,
    corpus_id: str,
    doc_id: str,
) -> None:
    """Delete Neo4j graph state for one document and prune shared edges."""
    async with driver.session() as session:
        affected_entity_ids = await _collect_distinct_entity_ids(
            session,
            corpus_id=corpus_id,
            doc_id=doc_id,
        )
        await _prune_relates_to_for_document(
            session,
            corpus_id=corpus_id,
            doc_id=doc_id,
        )
        while True:
            result = await session.run(
                """
            MATCH (n {doc_id: $doc_id, corpus_id: $corpus_id})
                WITH n LIMIT $batch_size
            DETACH DELETE n
                RETURN count(n) AS deleted
            """,
                doc_id=doc_id,
                corpus_id=corpus_id,
                batch_size=GRAPH_DELETE_BATCH_SIZE,
            )
            row = await result.single()
            if not row or int(row.get("deleted") or 0) == 0:
                break
        await _delete_orphan_entities(session)
        if affected_entity_ids:
            await _refresh_entity_aggregates(session, affected_entity_ids)


async def _clear_document_graph_payload(
    driver: AsyncDriver,
    *,
    corpus_id: str,
    doc_id: str,
) -> None:
    """Remove replaceable graph payload for one document/corpus before rewrite.

    ``write_document_graph`` is a replace operation from Mongo/Ghost B state.
    MERGE keeps identical rows idempotent, but it cannot remove stale Chunk,
    Fact, MENTION, or HAS_CHUNK rows left by a prior retry with a different
    chunk set. Keep the Document anchor and clear only corpus-scoped payload.
    """
    async with driver.session() as session:
        affected_entity_ids = await _collect_distinct_entity_ids(
            session,
            corpus_id=corpus_id,
            doc_id=doc_id,
        )
        await _prune_relates_to_for_document(
            session,
            corpus_id=corpus_id,
            doc_id=doc_id,
        )
        while True:
            result = await session.run(
                """
            MATCH (n {doc_id: $doc_id, corpus_id: $corpus_id})
            WHERE NOT n:Document
                WITH n LIMIT $batch_size
            DETACH DELETE n
                RETURN count(n) AS deleted
            """,
                doc_id=doc_id,
                corpus_id=corpus_id,
                batch_size=GRAPH_DELETE_BATCH_SIZE,
            )
            row = await result.single()
            if not row or int(row.get("deleted") or 0) == 0:
                break
        await _delete_orphan_entities(session)
        if affected_entity_ids:
            await _refresh_entity_aggregates(session, affected_entity_ids)


async def delete_corpus_graph(
    driver: AsyncDriver,
    *,
    corpus_id: str,
) -> None:
    """Delete Neo4j graph state for one corpus and prune shared edges.

    Batched: a single DETACH DELETE over a large corpus (~1.7M nodes+edges on
    a 496-book corpus) runs one giant transaction that exceeds the proxy
    timeout AND can exhaust Neo4j's heap. Named bounded auto-commit batches
    keep each transaction small — loop until none remain."""
    async with driver.session() as session:
        affected_entity_ids = await _collect_distinct_entity_ids(
            session,
            corpus_id=corpus_id,
        )
        await _prune_relates_to_for_corpus(session, corpus_id=corpus_id)
        while True:
            res = await session.run(
                """
                MATCH (n {corpus_id: $corpus_id})
                WITH n LIMIT $batch_size
                DETACH DELETE n
                RETURN count(n) AS deleted
                """,
                corpus_id=corpus_id,
                batch_size=GRAPH_DELETE_BATCH_SIZE,
            )
            row = await res.single()
            if not row or int(row["deleted"]) == 0:
                break
        await _delete_orphan_entities(session)
        if affected_entity_ids:
            await _refresh_entity_aggregates(session, affected_entity_ids)


def _is_retryable_graph_write_transient(exc: TransientError) -> bool:
    code = str(getattr(exc, "code", "") or "")
    text = str(exc)
    return "DeadlockDetected" in code or "DeadlockDetected" in text


async def write_document_graph(*args: Any, **kwargs: Any) -> None:
    doc_id = str(kwargs.get("doc_id") or (args[1] if len(args) > 1 else ""))
    corpus_id = str(kwargs.get("corpus_id") or (args[2] if len(args) > 2 else ""))
    for attempt in range(1, GRAPH_WRITE_MAX_ATTEMPTS + 1):
        try:
            await _write_document_graph_once(*args, **kwargs)
            return
        except TransientError as exc:
            if (
                not _is_retryable_graph_write_transient(exc)
                or attempt >= GRAPH_WRITE_MAX_ATTEMPTS
            ):
                raise
            delay = GRAPH_WRITE_DEADLOCK_BACKOFF_SECONDS * attempt
            logger.warning(
                "Neo4j graph write deadlock; retrying doc=%s corpus=%s attempt=%d/%d delay=%.2fs",
                doc_id[:12],
                corpus_id[:8],
                attempt + 1,
                GRAPH_WRITE_MAX_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)


async def _write_document_graph_once(
    driver: AsyncDriver,
    doc_id: str,
    corpus_id: str,
    extraction_results: list[ExtractionResult],
    user_id: str | None = None,
    file_id: str | None = None,
    all_chunk_ids: list[str] | None = None,
    *,
    filename: str | None = None,
    parent_count: int = 0,
    source_path: str | None = None,
    source_tier: str | None = None,
    schema_lens_id: str | None = None,
    ghost_b_success_rate: float | None = None,
    ghost_b_extracted: int | None = None,
    ghost_b_total: int | None = None,
    db: Any | None = None,
    chunk_parent_ids: dict[str, str] | None = None,
) -> None:
    """
    Write the full graph for one document after GHOST B completes.

    Phase K — BATCHED via UNWIND. Previously this fired one MERGE per
    chunk / entity / relation, producing 500-3000 serial round-trips per
    document. The new version executes 4 queries total per document
    (Document, Chunks, Entities+Mentions, Relations) regardless of size,
    reducing Neo4j round-trips by ~1000x on busy docs.

    Creates/updates: Document node, Chunk nodes, HAS_CHUNK edges,
    Entity nodes, MENTIONS edges, RELATES_TO edges (entity→entity only),
    plus optional Fact nodes linked by HAS_FACT and SUPPORTS_FACT.
    all_chunk_ids lets ingestion preserve complete document/chunk coverage in
    Neo4j even when Ghost B only returned partial entity/relation extraction.

    Brain View anchor properties: `filename`, `parent_count`, `source_*`,
    `schema_lens_id`, and the three flat ghost_b_* metrics are forwarded to
    the rich `_upsert_document` so the Document node serves as a cluster
    anchor for the books-as-clusters view. `chunk_count` is auto-derived
    from `all_chunk_ids` + the extraction-result chunk ids.
    """
    await _clear_document_graph_payload(
        driver,
        corpus_id=corpus_id,
        doc_id=doc_id,
    )

    # 1. Document node — rich anchor MERGE with cluster-anchor flags. When the
    # caller provides all_chunk_ids, Mongo's current chunk table is the source
    # of truth. Retry/resume can leave stale Ghost B rows for old chunk IDs; do
    # not reintroduce those as HAS_CHUNK edges.
    if all_chunk_ids is not None:
        canonical_chunk_ids = list(dict.fromkeys(all_chunk_ids))
        allowed_chunk_ids = set(canonical_chunk_ids)
        original_result_count = len(extraction_results)
        extraction_results = [
            result
            for result in extraction_results
            if result.chunk_id in allowed_chunk_ids
        ]
        dropped_stale_results = original_result_count - len(extraction_results)
        if dropped_stale_results:
            logger.warning(
                "Neo4j graph write dropped stale extraction rows: doc=%s corpus=%s dropped=%d",
                doc_id[:12],
                corpus_id[:8],
                dropped_stale_results,
            )
        chunk_ids = canonical_chunk_ids
    else:
        chunk_ids = list(dict.fromkeys([r.chunk_id for r in extraction_results]))
    # Pt 6 scaling fix: compute dominant family + entity type here from the
    # in-memory extraction results, write to the Document node. Brain View
    # Cypher then reads the property instead of OPTIONAL MATCH'ing every
    # chunk × entity per anchor per query.
    dom_family, dom_type = summarize_dominant_facets(extraction_results)
    await _upsert_document(
        driver,
        doc_id,
        corpus_id,
        user_id,
        file_id,
        filename=filename,
        chunk_count=len(chunk_ids),
        parent_count=parent_count,
        source_path=source_path,
        source_tier=source_tier,
        schema_lens_id=schema_lens_id,
        ghost_b_success_rate=ghost_b_success_rate,
        ghost_b_extracted=ghost_b_extracted,
        ghost_b_total=ghost_b_total,
        dominant_family=dom_family,
        dominant_entity_type=dom_type,
    )

    # 2. Build batched payloads from the extraction results. `chunk_ids` was
    # computed above for the anchor's chunk_count — reuse it for the chunk rows.
    chunk_rows: list[dict] = [{"chunk_id": chunk_id} for chunk_id in chunk_ids]

    entity_groups: dict[str, dict] = {}
    skipped_junk_entities = 0
    for result in extraction_results:
        # Pt 10b — per-result chunk text, used to seed `text_context` for
        # taxonomy synonym matching at resolve time. See note below.
        result_text = getattr(result, "text", "") or ""
        for entity in result.entities:
            if is_junk_extracted_entity(entity.canonical_name, entity.surface_form):
                skipped_junk_entities += 1
                continue
            canonical = canonicalize_entity_name(entity.canonical_name)
            if not canonical:
                continue
            group = entity_groups.setdefault(
                canonical,
                {
                    "canonical_name": canonical,
                    "display_name": entity.surface_form or entity.canonical_name,
                    "observed_entity_types": [],
                    "confidence": 0.0,
                    # Pt 10b — collect distinct chunk texts where this entity
                    # appears. Capped at 3 to keep the resolver haystack
                    # bounded (substring matching is O(haystack × terms)).
                    "text_chunks": [],
                    # Pt 10c — union of query_aliases across all mentions
                    # (deduped, case-insensitive). Cap 8 — slightly above the
                    # per-mention cap of 5 to allow different chunks to
                    # contribute distinct variants.
                    "query_aliases": [],
                    # Pt 10c — first non-empty definitional phrase, sourced
                    # from the highest-confidence mention. We pick early-bind:
                    # whichever mention raises the group's confidence high-
                    # water mark and has a non-empty phrase wins.
                    "definitional_phrase": "",
                    # Pt9b — LLM-emitted object_kind. First non-empty value
                    # from the highest-confidence mention wins (same policy
                    # as definitional_phrase). Empty here → fall back to
                    # resolve_facets() heuristic inference at write time.
                    "llm_object_kind": "",
                },
            )
            if entity.entity_type not in group["observed_entity_types"]:
                group["observed_entity_types"].append(entity.entity_type)
            if entity.confidence > group["confidence"]:
                group["confidence"] = entity.confidence
                group["display_name"] = entity.surface_form or entity.canonical_name
                # Pt 10c — promote definitional_phrase from the new highest-
                # confidence mention if it has one. Stickier than "first
                # seen" because high-confidence mentions tend to come from
                # more definitional surrounding text.
                phrase = (getattr(entity, "definitional_phrase", "") or "").strip()
                if phrase and not group["definitional_phrase"]:
                    group["definitional_phrase"] = phrase[:200]
                # Pt9b — promote LLM-emitted object_kind from the new
                # highest-confidence mention. Sticky for the same reason
                # as definitional_phrase: high-confidence mentions are
                # closer to ground truth.
                kind = (getattr(entity, "object_kind", "") or "").strip()
                if kind and not group["llm_object_kind"]:
                    group["llm_object_kind"] = kind[:100]
            # Pt 10b — gather context for taxonomy resolution. Dedupe by
            # exact text (same chunk may surface the entity multiple times).
            if (
                result_text
                and result_text not in group["text_chunks"]
                and len(group["text_chunks"]) < 3
            ):
                group["text_chunks"].append(result_text)
            # Pt 10c — union query_aliases case-insensitively. Skip aliases
            # equal to the canonical or display name (no signal value).
            for alias in getattr(entity, "query_aliases", None) or []:
                alias_clean = str(alias).strip()
                if not alias_clean or len(group["query_aliases"]) >= 8:
                    continue
                alias_lc = alias_clean.lower()
                if alias_lc == canonical.lower():
                    continue
                if any(a.lower() == alias_lc for a in group["query_aliases"]):
                    continue
                group["query_aliases"].append(alias_clean)

    entity_identity: dict[str, dict] = {}
    for canonical, group in entity_groups.items():
        observed_types = list(group["observed_entity_types"])
        primary_type = resolve_primary_entity_type(canonical, observed_types)
        # Pt 10b — concatenate the up-to-3 chunk texts as `text_context`.
        # Pre-fix this defaulted to "" and synonym matching in the taxonomy
        # resolvers fell through for ~99% of entities. Production data:
        # only 1.2% of Entity nodes had object_kind populated, 5.3% had
        # domain_type, 0.6% had canonical_family — and those were almost
        # entirely Products (exact-name hits), Documents (.pdf/.doc), and
        # Artifacts (.dll/.so). Concept / Method / Organization / Person
        # entities were starved of context and ended up with empty ontology.
        text_context = " ".join(group["text_chunks"])
        ontology = resolve_ontology_metadata(canonical, primary_type, text_context)
        # Pt9b — LLM-emitted object_kind beats heuristic inference when
        # present. resolve_facets only fires for 3 entity_types (Artifact,
        # Product, Document) and only matches ~1.2% of names in practice.
        # The LLM, having actually read the chunk text, makes better calls.
        # Fall back to ontology["object_kind"] when the LLM didn't emit one
        # (older corpora ingested pre-Pt9b, or chunks where the LLM omitted
        # the optional field).
        llm_kind = (group.get("llm_object_kind") or "").strip()
        effective_object_kind = llm_kind or ontology.get("object_kind")
        entity_identity[canonical] = {
            "entity_id": entity_id_from_name(canonical, primary_type),
            "canonical_name": canonical,
            "display_name": group["display_name"],
            "primary_entity_type": primary_type,
            "observed_entity_types": observed_types,
            "confidence": group["confidence"],
            "object_kind": effective_object_kind,
            "object_kind_parent": ontology.get("object_kind_parent"),
            "object_kind_root": ontology.get("object_kind_root"),
            "domain_type": ontology.get("domain_type"),
            "domain_type_parent": ontology.get("domain_type_parent"),
            "domain_type_root": ontology.get("domain_type_root"),
            "canonical_family": ontology.get("canonical_family"),
            "ontology_version": ontology.get("ontology_version"),
            "generic_entity": is_generic_entity_name(canonical),
            # Pt 10c — query-facing fields for Mode B entity search and
            # chat-citation context. Both default-safe (empty list / "") on
            # pre-Pt-10c entities so existing data + Cypher coalesces work
            # without migration.
            "query_aliases": group.get("query_aliases") or [],
            "definitional_phrase": group.get("definitional_phrase") or "",
        }

    mention_rows: list[dict] = []
    alias_resolution_count = 0
    facet_resolution_count = 0
    domain_resolution_count = 0
    family_resolution_count = 0
    for r in extraction_results:
        for entity in r.entities:
            normalized = normalize_entity_name(entity.canonical_name)
            canonical_normalized = resolve_entity_alias(normalized)
            identity = entity_identity.get(canonical_normalized)
            if not identity:
                continue
            if canonical_normalized != normalized:
                alias_resolution_count += 1
            if identity.get("object_kind"):
                facet_resolution_count += 1
            if identity.get("domain_type"):
                domain_resolution_count += 1
            if identity.get("canonical_family"):
                family_resolution_count += 1
            mention_rows.append(
                {
                    "chunk_id": r.chunk_id,
                    "entity_id": identity["entity_id"],
                    "normalized_name": canonical_normalized,
                    "canonical_name": identity["canonical_name"],
                    "display_name": identity["display_name"],
                    "generic_entity": identity["generic_entity"],
                    "surface_form": entity.surface_form or entity.canonical_name,
                    # PRD parity — keep the chunk-grounded evidence phrase on the
                    # MENTIONS edge so downstream retrieval can render "this is
                    # WHY we think the chunk mentions this entity".
                    "evidence_phrase": getattr(entity, "evidence_phrase", None),
                    "primary_entity_type": identity["primary_entity_type"],
                    "entity_type": identity["primary_entity_type"],
                    "extracted_type": entity.entity_type,
                    "observed_entity_types": identity["observed_entity_types"],
                    "confidence": entity.confidence,
                    "object_kind": identity.get("object_kind"),
                    "object_kind_parent": identity.get("object_kind_parent"),
                    "object_kind_root": identity.get("object_kind_root"),
                    "domain_type": identity.get("domain_type"),
                    "domain_type_parent": identity.get("domain_type_parent"),
                    "domain_type_root": identity.get("domain_type_root"),
                    "canonical_family": identity.get("canonical_family"),
                    "ontology_version": identity.get("ontology_version"),
                    # Pt 10c — query-facing fields. Identity dict already
                    # aggregated these across mentions; pass through to the
                    # UNWIND row so the Cypher SET CASE-merge can additively
                    # accumulate aliases and stash the definitional phrase.
                    "query_aliases": identity.get("query_aliases") or [],
                    "definitional_phrase": identity.get("definitional_phrase") or "",
                }
            )

    relation_rows: list[dict] = []
    related_to_refinement_count = 0
    skipped_relations_missing_endpoint = 0
    for r in extraction_results:
        for relation in r.relations:
            if relation.object_kind != "entity":
                continue
            source_predicate_raw = relation.source_predicate or relation.predicate
            (
                normalized_source_predicate,
                reverse_relation,
            ) = normalize_relation_predicate_alias(source_predicate_raw)
            subject_name = relation.object if reverse_relation else relation.subject
            object_name = relation.subject if reverse_relation else relation.object
            subject_canonical = canonicalize_entity_name(subject_name)
            object_canonical = canonicalize_entity_name(object_name)
            subject_identity = entity_identity.get(subject_canonical)
            object_identity = entity_identity.get(object_canonical)
            if not subject_identity or not object_identity:
                skipped_relations_missing_endpoint += 1
                continue
            refined_predicate = refine_related_to_predicate(
                relation.predicate,
                subject_identity,
                object_identity,
                source_predicate=normalized_source_predicate,
                evidence_phrase=relation.evidence_phrase,
                relation_cue=relation.relation_cue,
            )
            if refined_predicate != relation.predicate:
                related_to_refinement_count += 1
            predicate_refined = refined_predicate != relation.predicate
            validation_status = relation.validation_status or (
                "repaired_from_related_to" if predicate_refined else None
            )
            if reverse_relation:
                validation_status = (
                    f"{validation_status}+direction_repair"
                    if validation_status
                    else "direction_repair"
                )
            edge_strength = relation_edge_strength(
                refined_predicate,
                relation.confidence,
                validation_status,
                predicate_refined=predicate_refined,
            )
            edge_mitigation = build_relation_edge_mitigation(
                extracted_predicate=relation.predicate,
                refined_predicate=refined_predicate,
                source_predicate=source_predicate_raw,
                confidence=relation.confidence,
                evidence_phrase=relation.evidence_phrase,
                relation_cue=relation.relation_cue,
                predicate_refined=predicate_refined,
            )
            relation_rows.append(
                {
                    "subject_id": subject_identity["entity_id"],
                    "object_id": object_identity["entity_id"],
                    "predicate": refined_predicate,
                    "schema_version": r.schema_version,
                    "source_predicate": source_predicate_raw,
                    "relation_family": edge_mitigation.relation_family,
                    "predicate_refined": predicate_refined,
                    "direction_repaired": reverse_relation,
                    "edge_strength": edge_strength,
                    "eligible_for_synthesis": relation_eligible_for_synthesis(
                        refined_predicate, relation.confidence, validation_status
                    ),
                    "edge_state": edge_mitigation.edge_state,
                    "fallback": edge_mitigation.fallback,
                    "fallback_family": edge_mitigation.fallback_family,
                    "candidate_predicates": edge_mitigation.candidate_predicates,
                    "candidate_scores": edge_mitigation.candidate_scores,
                    "candidate_score_sources": edge_mitigation.candidate_score_sources,
                    "promoted_by": edge_mitigation.promoted_by,
                    "fallback_evidence_phrase": edge_mitigation.fallback_evidence_phrase,
                    "related_to_query_weight": edge_mitigation.related_to_query_weight,
                    "related_to_max_hops": edge_mitigation.related_to_max_hops,
                    "validation_status": validation_status,
                    "evidence_phrase": relation.evidence_phrase,
                    "relation_cue": relation.relation_cue,
                    "confidence": relation.confidence,
                    "chunk_id": r.chunk_id,
                    "doc_id": r.doc_id or doc_id,
                    "chunk_key": corpus_content_key(corpus_id, r.chunk_id),
                    "doc_key": corpus_content_key(corpus_id, r.doc_id or doc_id),
                }
            )

    fact_rows: list[dict] = []
    skipped_facts_missing_subject = 0
    for r in extraction_results:
        for fact in getattr(r, "facts", []) or []:
            subject_canonical = canonicalize_entity_name(fact.subject)
            subject_identity = entity_identity.get(subject_canonical)
            if not subject_identity:
                skipped_facts_missing_subject += 1
                continue
            fact_rows.append(
                {
                    "fact_id": fact_id_from_parts(
                        doc_id=doc_id,
                        chunk_id=r.chunk_id,
                        subject=fact.subject,
                        property_name=fact.property_name,
                        value=fact.value,
                    ),
                    "subject_entity_id": subject_identity["entity_id"],
                    "subject": subject_identity["canonical_name"],
                    "doc_id": doc_id,
                    "chunk_id": r.chunk_id,
                    "fact_type": fact.fact_type,
                    "property_name": fact.property_name,
                    "value": fact.value,
                    "unit": fact.unit,
                    "condition": fact.condition,
                    "confidence": fact.confidence,
                    "evidence_phrase": fact.evidence_phrase,
                }
            )

    if alias_resolution_count:
        logger.info(
            "Neo4j alias resolution: doc=%s corpus=%s aliases=%d",
            doc_id[:12],
            corpus_id[:8],
            alias_resolution_count,
        )
    if facet_resolution_count or domain_resolution_count or family_resolution_count:
        logger.info(
            "Neo4j ontology resolution: doc=%s corpus=%s object_kind=%d domain_type=%d family=%d version=%s",
            doc_id[:12],
            corpus_id[:8],
            facet_resolution_count,
            domain_resolution_count,
            family_resolution_count,
            ONTOLOGY_VERSION,
        )
    if related_to_refinement_count:
        logger.info(
            "Neo4j relation refinement: doc=%s corpus=%s related_to_refined=%d",
            doc_id[:12],
            corpus_id[:8],
            related_to_refinement_count,
        )
    if (
        skipped_junk_entities
        or skipped_relations_missing_endpoint
        or skipped_facts_missing_subject
    ):
        logger.info(
            "Neo4j graph cleaning: doc=%s corpus=%s junk_entities=%d dropped_relations=%d dropped_facts=%d",
            doc_id[:12],
            corpus_id[:8],
            skipped_junk_entities,
            skipped_relations_missing_endpoint,
            skipped_facts_missing_subject,
        )

    # 3. Single session for all remaining writes. Every UNWIND is sliced into
    # a named, transaction-bounded batch.
    async with driver.session() as session:
        await _redirect_graph_write_rows(
            session,
            mention_rows=mention_rows,
            relation_rows=relation_rows,
            fact_rows=fact_rows,
        )

        if db is not None:
            try:
                from services.storage.mongo_writer import (
                    replace_relation_support_for_document,
                )

                support_records = _relation_support_records(
                    relation_rows=relation_rows,
                    corpus_id=corpus_id,
                    doc_id=doc_id,
                    chunk_parent_ids=chunk_parent_ids,
                )
                support_count = await replace_relation_support_for_document(
                    db,
                    doc_id=doc_id,
                    corpus_id=corpus_id,
                    records=support_records,
                )
                logger.info(
                    "Mongo relation support records refreshed: doc=%s corpus=%s count=%d",
                    doc_id[:12],
                    corpus_id[:8],
                    support_count,
                )
            except Exception:
                logger.exception(
                    "Mongo relation support record refresh failed: doc=%s corpus=%s",
                    doc_id[:12],
                    corpus_id[:8],
                )

        # Chunks + HAS_CHUNK edges.
        for row_batch in _row_batches(
            chunk_rows,
            batch_size=GRAPH_WRITE_ROW_BATCH_SIZE,
        ):
            await session.run(
                """
                UNWIND $rows AS row
                MERGE (c:Chunk {corpus_id: $corpus_id, chunk_id: row.chunk_id})
                SET c.doc_id = $doc_id
                WITH c
                MATCH (d:Document {doc_id: $doc_id, corpus_id: $corpus_id})
                MERGE (d)-[:HAS_CHUNK]->(c)
                """,
                rows=row_batch,
                doc_id=doc_id,
                corpus_id=corpus_id,
            )

        # Entities + MENTIONS edges.
        for row_batch in _row_batches(
            mention_rows,
            batch_size=GRAPH_WRITE_ROW_BATCH_SIZE,
        ):
            await session.run(
                """
                UNWIND $rows AS row
                MERGE (e:Entity {entity_id: row.entity_id})
                ON CREATE SET e.first_seen = timestamp()
                SET e.normalized_name = CASE
                        WHEN row.resolved_from_entity_id IS NULL THEN row.normalized_name
                        ELSE coalesce(e.normalized_name, row.normalized_name)
                    END,
                    e.canonical_name = CASE
                        WHEN row.resolved_from_entity_id IS NULL THEN row.canonical_name
                        ELSE coalesce(e.canonical_name, row.canonical_name)
                    END,
                    e.display_name = CASE
                        WHEN row.resolved_from_entity_id IS NULL THEN row.display_name
                        ELSE coalesce(e.display_name, row.display_name)
                    END,
                    e.primary_entity_type = CASE
                        WHEN row.resolved_from_entity_id IS NULL THEN row.primary_entity_type
                        ELSE coalesce(e.primary_entity_type, row.primary_entity_type)
                    END,
                    e.entity_type = CASE
                        WHEN row.resolved_from_entity_id IS NULL THEN row.primary_entity_type
                        ELSE coalesce(e.entity_type, row.primary_entity_type)
                    END,
                    e.confidence = CASE
                        WHEN e.confidence IS NULL OR row.confidence > e.confidence THEN row.confidence
                        ELSE e.confidence
                    END,
                    e.object_kind = CASE
                        WHEN row.resolved_from_entity_id IS NULL THEN row.object_kind
                        ELSE coalesce(e.object_kind, row.object_kind)
                    END,
                    e.object_kind_parent = CASE
                        WHEN row.resolved_from_entity_id IS NULL THEN row.object_kind_parent
                        ELSE coalesce(e.object_kind_parent, row.object_kind_parent)
                    END,
                    e.object_kind_root = CASE
                        WHEN row.resolved_from_entity_id IS NULL THEN row.object_kind_root
                        ELSE coalesce(e.object_kind_root, row.object_kind_root)
                    END,
                    e.domain_type = CASE
                        WHEN row.resolved_from_entity_id IS NULL THEN row.domain_type
                        ELSE coalesce(e.domain_type, row.domain_type)
                    END,
                    e.domain_type_parent = CASE
                        WHEN row.resolved_from_entity_id IS NULL THEN row.domain_type_parent
                        ELSE coalesce(e.domain_type_parent, row.domain_type_parent)
                    END,
                    e.domain_type_root = CASE
                        WHEN row.resolved_from_entity_id IS NULL THEN row.domain_type_root
                        ELSE coalesce(e.domain_type_root, row.domain_type_root)
                    END,
                    e.canonical_family = CASE
                        WHEN row.resolved_from_entity_id IS NULL THEN row.canonical_family
                        ELSE coalesce(e.canonical_family, row.canonical_family)
                    END,
                    e.ontology_version = CASE
                        WHEN row.resolved_from_entity_id IS NULL THEN row.ontology_version
                        ELSE coalesce(e.ontology_version, row.ontology_version)
                    END,
                    e.query_aliases = CASE
                        WHEN row.query_aliases IS NULL OR size(row.query_aliases) = 0 THEN coalesce(e.query_aliases, [])
                        ELSE [a IN row.query_aliases WHERE NOT a IN coalesce(e.query_aliases, [])] + coalesce(e.query_aliases, [])
                    END,
                    e.definitional_phrase = CASE
                        WHEN row.definitional_phrase IS NULL OR row.definitional_phrase = '' THEN coalesce(e.definitional_phrase, '')
                        WHEN coalesce(e.definitional_phrase, '') = '' THEN row.definitional_phrase
                        ELSE e.definitional_phrase
                    END,
                    e.generic_entity = coalesce(e.generic_entity, false) OR row.generic_entity,
                    e.ambiguous = coalesce(e.ambiguous, false) OR row.generic_entity,
                    e.needs_review = coalesce(e.needs_review, false) OR row.generic_entity,
                    e.graph_expansion_allowed = CASE
                        WHEN row.generic_entity THEN false
                        ELSE coalesce(e.graph_expansion_allowed, true)
                    END
                WITH e, row
                SET e.observed_entity_types = reduce(
                    types = coalesce(e.observed_entity_types, []),
                    t IN row.observed_entity_types |
                    CASE WHEN t IN types THEN types ELSE types + [t] END
                )
                WITH e, row
                MATCH (c:Chunk {chunk_id: row.chunk_id, corpus_id: $corpus_id})
                MERGE (c)-[m:MENTIONS]->(e)
                SET m.confidence = CASE
                        WHEN m.confidence IS NULL OR row.confidence > m.confidence THEN row.confidence
                        ELSE m.confidence
                    END,
                    m.extracted_type = row.extracted_type,
                    m.surface_form = row.surface_form,
                    m.evidence_phrase = coalesce(row.evidence_phrase, m.evidence_phrase),
                    m.extractor = 'ghost_b',
                    m.ontology_version = row.ontology_version,
                    m.corpus_id = $corpus_id,
                    m.doc_id = c.doc_id
                SET m.extracted_types = CASE
                    WHEN m.extracted_types IS NULL THEN [row.extracted_type]
                    WHEN row.extracted_type IN m.extracted_types THEN m.extracted_types
                    ELSE m.extracted_types + [row.extracted_type]
                END
                """,
                rows=row_batch,
                corpus_id=corpus_id,
            )

        # RELATES_TO edges between entities (entity_id lookup).
        for row_batch in _row_batches(
            relation_rows,
            batch_size=GRAPH_WRITE_ROW_BATCH_SIZE,
        ):
            await session.run(
                """
                UNWIND $rows AS row
                MATCH (s:Entity {entity_id: row.subject_id})
                MATCH (o:Entity {entity_id: row.object_id})
                MERGE (s)-[r:RELATES_TO {predicate: row.predicate}]->(o)
                WITH r, row,
                     row.chunk_key IN coalesce(r.evidence_chunk_keys, []) AS chunk_already_supported,
                     row.chunk_key IN coalesce(r.support_confidence_chunk_keys, []) AS confidence_already_supported
                SET r.confidence = CASE
                        WHEN r.confidence IS NULL OR row.confidence > r.confidence THEN row.confidence
                        ELSE r.confidence
                    END,
                    r.relation_family = row.relation_family,
                    r.predicate_refined = coalesce(r.predicate_refined, false) OR row.predicate_refined,
                    r.direction_repaired = coalesce(r.direction_repaired, false) OR row.direction_repaired,
                    r.edge_state = CASE
                        WHEN row.edge_state IN ['typed', 'refined'] THEN row.edge_state
                        WHEN coalesce(r.edge_state, '') IN ['', 'fallback'] AND row.edge_state = 'family' THEN 'family'
                        WHEN coalesce(r.edge_state, '') = '' THEN row.edge_state
                        ELSE r.edge_state
                    END,
                    r.fallback = row.fallback,
                    r.fallback_family = CASE
                        WHEN row.fallback_family IS NULL OR row.fallback_family = '' THEN coalesce(r.fallback_family, '')
                        ELSE row.fallback_family
                    END,
                    r.promoted_by = CASE
                        WHEN row.promoted_by IS NULL OR row.promoted_by = '' THEN coalesce(r.promoted_by, '')
                        ELSE row.promoted_by
                    END,
                    r.related_to_query_weight = toFloat(coalesce(row.related_to_query_weight, CASE WHEN row.predicate = 'related_to' THEN 0.5 ELSE 1.0 END)),
                    r.related_to_max_hops = toInteger(coalesce(row.related_to_max_hops, CASE WHEN row.predicate = 'related_to' THEN 1 ELSE 2 END)),
                    r.edge_strength = CASE
                        WHEN row.edge_strength = 'strong' THEN 'strong'
                        WHEN row.edge_strength = 'repaired' AND coalesce(r.edge_strength, '') <> 'strong' THEN 'repaired'
                        WHEN row.edge_strength = 'thin' AND coalesce(r.edge_strength, '') IN ['', 'weak'] THEN 'thin'
                        ELSE coalesce(r.edge_strength, row.edge_strength)
                    END,
                    r.eligible_for_synthesis = coalesce(r.eligible_for_synthesis, false) OR row.eligible_for_synthesis
                SET r.evidence_chunk_ids = CASE
                    WHEN r.evidence_chunk_ids IS NULL THEN [row.chunk_id]
                    WHEN row.chunk_id IN r.evidence_chunk_ids THEN r.evidence_chunk_ids
                    ELSE r.evidence_chunk_ids + [row.chunk_id]
                END
                SET r.evidence_chunk_keys = CASE
                    WHEN r.evidence_chunk_keys IS NULL THEN [row.chunk_key]
                    WHEN row.chunk_key IN r.evidence_chunk_keys THEN r.evidence_chunk_keys
                    ELSE r.evidence_chunk_keys + [row.chunk_key]
                END
                SET r.evidence_doc_ids = CASE
                    WHEN row.doc_id IS NULL OR row.doc_id = '' THEN coalesce(r.evidence_doc_ids, [])
                    WHEN r.evidence_doc_ids IS NULL THEN [row.doc_id]
                    WHEN row.doc_id IN r.evidence_doc_ids THEN r.evidence_doc_ids
                    ELSE r.evidence_doc_ids + [row.doc_id]
                END,
                    r.evidence_doc_keys = CASE
                    WHEN row.doc_key IS NULL OR row.doc_key = '' THEN coalesce(r.evidence_doc_keys, [])
                    WHEN r.evidence_doc_keys IS NULL THEN [row.doc_key]
                    WHEN row.doc_key IN r.evidence_doc_keys THEN r.evidence_doc_keys
                    ELSE r.evidence_doc_keys + [row.doc_key]
                END,
                    r.latest_doc_id = CASE
                        WHEN row.doc_id IS NULL OR row.doc_id = '' THEN r.latest_doc_id
                        ELSE row.doc_id
                    END,
                    r.latest_doc_key = CASE
                        WHEN row.doc_key IS NULL OR row.doc_key = '' THEN r.latest_doc_key
                        ELSE row.doc_key
                    END
                SET r.evidence_phrases = CASE
                        WHEN row.evidence_phrase IS NULL OR row.evidence_phrase = '' THEN coalesce(r.evidence_phrases, [])
                        WHEN r.evidence_phrases IS NULL THEN [row.evidence_phrase]
                        WHEN row.evidence_phrase IN r.evidence_phrases THEN r.evidence_phrases
                        ELSE r.evidence_phrases + [row.evidence_phrase]
                    END,
                        r.fallback_evidence_phrase = CASE
                        WHEN row.fallback_evidence_phrase IS NULL OR row.fallback_evidence_phrase = '' THEN coalesce(r.fallback_evidence_phrase, '')
                        ELSE row.fallback_evidence_phrase
                    END
                    SET r.relation_cues = CASE
                        WHEN row.relation_cue IS NULL OR row.relation_cue = '' THEN coalesce(r.relation_cues, [])
                        WHEN r.relation_cues IS NULL THEN [row.relation_cue]
                        WHEN row.relation_cue IN r.relation_cues THEN r.relation_cues
                        ELSE r.relation_cues + [row.relation_cue]
                END
                SET r.source_predicates = CASE
                        WHEN r.source_predicates IS NULL THEN [row.source_predicate]
                        WHEN row.source_predicate IN r.source_predicates THEN r.source_predicates
                        ELSE r.source_predicates + [row.source_predicate]
                    END
                    SET r.candidate_predicates = CASE
                        WHEN size(coalesce(row.candidate_predicates, [])) = 0 THEN coalesce(r.candidate_predicates, [])
                        WHEN r.candidate_predicates IS NULL THEN row.candidate_predicates
                        ELSE reduce(candidates = r.candidate_predicates, candidate IN row.candidate_predicates |
                            CASE WHEN candidate IN candidates THEN candidates ELSE candidates + candidate END)
                    END,
                        r.candidate_scores = CASE
                        WHEN size(coalesce(row.candidate_scores, [])) = 0 THEN coalesce(r.candidate_scores, [])
                        ELSE row.candidate_scores
                    END,
                        r.candidate_score_sources = CASE
                        WHEN size(coalesce(row.candidate_score_sources, [])) = 0 THEN coalesce(r.candidate_score_sources, [])
                        ELSE row.candidate_score_sources
                    END
                    SET r.validation_statuses = CASE
                    WHEN row.validation_status IS NULL OR row.validation_status = '' THEN coalesce(r.validation_statuses, [])
                    WHEN r.validation_statuses IS NULL THEN [row.validation_status]
                    WHEN row.validation_status IN r.validation_statuses THEN r.validation_statuses
                    ELSE r.validation_statuses + [row.validation_status]
                END
                SET r.corpus_ids = CASE
                    WHEN r.corpus_ids IS NULL THEN [$corpus_id]
                    WHEN $corpus_id IN r.corpus_ids THEN r.corpus_ids
                    ELSE r.corpus_ids + [$corpus_id]
                END
                SET r.support_confidence_chunk_ids = CASE
                    WHEN row.chunk_id IS NULL OR row.chunk_id = '' THEN coalesce(r.support_confidence_chunk_ids, [])
                    WHEN chunk_already_supported OR confidence_already_supported THEN coalesce(r.support_confidence_chunk_ids, [])
                    WHEN r.support_confidence_chunk_ids IS NULL THEN [row.chunk_id]
                    ELSE r.support_confidence_chunk_ids + [row.chunk_id]
                END,
                    r.support_confidence_chunk_keys = CASE
                    WHEN row.chunk_key IS NULL OR row.chunk_key = '' THEN coalesce(r.support_confidence_chunk_keys, [])
                    WHEN chunk_already_supported OR confidence_already_supported THEN coalesce(r.support_confidence_chunk_keys, [])
                    WHEN r.support_confidence_chunk_keys IS NULL THEN [row.chunk_key]
                    ELSE r.support_confidence_chunk_keys + [row.chunk_key]
                END,
                    r.support_confidence_values = CASE
                    WHEN row.chunk_id IS NULL OR row.chunk_id = '' THEN coalesce(r.support_confidence_values, [])
                    WHEN chunk_already_supported OR confidence_already_supported THEN coalesce(r.support_confidence_values, [])
                    WHEN r.support_confidence_values IS NULL THEN [toFloat(coalesce(row.confidence, 0.0))]
                    ELSE r.support_confidence_values + [toFloat(coalesce(row.confidence, 0.0))]
                END,
                    r.support_confidence_values_v2 = CASE
                    WHEN row.chunk_key IS NULL OR row.chunk_key = '' THEN coalesce(r.support_confidence_values_v2, [])
                    WHEN chunk_already_supported OR confidence_already_supported THEN coalesce(r.support_confidence_values_v2, [])
                    WHEN r.support_confidence_values_v2 IS NULL THEN [toFloat(coalesce(row.confidence, 0.0))]
                    ELSE r.support_confidence_values_v2 + [toFloat(coalesce(row.confidence, 0.0))]
                END
                SET r.extract_schema_versions = CASE
                    WHEN row.schema_version IS NULL OR row.schema_version = '' THEN coalesce(r.extract_schema_versions, [])
                    WHEN r.extract_schema_versions IS NULL THEN [row.schema_version]
                    WHEN row.schema_version IN r.extract_schema_versions THEN r.extract_schema_versions
                    ELSE r.extract_schema_versions + [row.schema_version]
                END
                SET r.support_count = CASE
                        WHEN size(coalesce(r.evidence_chunk_keys, [])) > size(coalesce(r.evidence_chunk_ids, []))
                        THEN size(coalesce(r.evidence_chunk_keys, []))
                        ELSE size(coalesce(r.evidence_chunk_ids, []))
                    END,
                    r.avg_confidence = CASE
                        WHEN size(coalesce(r.support_confidence_values_v2, [])) > 0
                        THEN reduce(total = 0.0, conf IN coalesce(r.support_confidence_values_v2, []) | total + toFloat(conf))
                             / size(coalesce(r.support_confidence_values_v2, []))
                        WHEN size(coalesce(r.support_confidence_values, [])) > 0
                        THEN reduce(total = 0.0, conf IN coalesce(r.support_confidence_values, []) | total + toFloat(conf))
                             / size(coalesce(r.support_confidence_values, []))
                        ELSE toFloat(coalesce(r.confidence, row.confidence, 0.0))
                    END,
                    r.extract_schema_version = coalesce(r.extract_schema_version, row.schema_version, 'polymath.extract.v1'),
                    r.promote_version = $promote_version,
                    r.last_seen_at = timestamp()
                """,
                rows=row_batch,
                corpus_id=corpus_id,
                promote_version=GRAPH_PROMOTE_VERSION,
            )
            await session.run(
                """
                UNWIND $rows AS row
                WITH row
                WHERE row.predicate <> 'related_to'
                MATCH (s:Entity {entity_id: row.subject_id})
                MATCH (o:Entity {entity_id: row.object_id})
                MATCH (s)-[weak:RELATES_TO {predicate: 'related_to'}]-(o)
                DELETE weak
                """,
                rows=row_batch,
            )

        # Structured facts/properties stay separate from entity-to-entity edges.
        for row_batch in _row_batches(
            fact_rows,
            batch_size=GRAPH_WRITE_ROW_BATCH_SIZE,
        ):
            await session.run(
                """
                UNWIND $rows AS row
                MATCH (e:Entity {entity_id: row.subject_entity_id})
                MATCH (c:Chunk {chunk_id: row.chunk_id, corpus_id: $corpus_id})
                MERGE (f:Fact {corpus_id: $corpus_id, fact_id: row.fact_id})
                SET f.doc_id = row.doc_id,
                    f.chunk_id = row.chunk_id,
                    f.subject = row.subject,
                    f.fact_type = row.fact_type,
                    f.property_name = row.property_name,
                    f.value = row.value,
                    f.unit = row.unit,
                    f.condition = row.condition,
                    f.confidence = CASE
                        WHEN f.confidence IS NULL OR row.confidence > f.confidence THEN row.confidence
                        ELSE f.confidence
                    END,
                    f.evidence_phrase = row.evidence_phrase,
                    f.extractor = 'ghost_b',
                    f.updated_at = timestamp()
                MERGE (e)-[:HAS_FACT]->(f)
                MERGE (c)-[:SUPPORTS_FACT]->(f)
                """,
                rows=row_batch,
                corpus_id=corpus_id,
            )

        touched_entity_ids: list[str] = []
        touched_entity_ids.extend(row.get("entity_id", "") for row in mention_rows)
        for row in relation_rows:
            touched_entity_ids.append(row.get("subject_id", ""))
            touched_entity_ids.append(row.get("object_id", ""))
        touched_entity_ids.extend(row.get("subject_entity_id", "") for row in fact_rows)
        if touched_entity_ids:
            await _refresh_entity_aggregates(session, touched_entity_ids)

    entity_count = sum(len(r.entities) for r in extraction_results)
    relation_count = sum(len(r.relations) for r in extraction_results)
    fact_count = sum(len(getattr(r, "facts", []) or []) for r in extraction_results)
    logger.info(
        "Neo4j write complete (batched): doc=%s corpus=%s chunks=%d entities=%d relations=%d facts=%d",
        doc_id,
        corpus_id,
        len(chunk_rows),
        entity_count,
        relation_count,
        fact_count,
    )


async def write_graphify_enrichment(
    driver: AsyncDriver,
    *,
    corpus_id: str,
    enrichment,
) -> None:
    """Phase 4.5 — write graphify enrichment onto Phase 4's existing entities.

    Two side effects:
    1. Stamp `graphify_community` integer on `:Entity` nodes whose
       normalized name matches a graphify node's clean label.
    2. MERGE `(:Entity)-[:CALLS]->(:Entity)` edges for cross-symbol calls
       graphify detected. Confidence is fixed at 1.0 because the relation
       came from graphify's deterministic AST pass, not an LLM guess.

    Idempotent. Safe to re-run. No-op when `enrichment.is_empty`.
    """
    if enrichment is None or enrichment.is_empty:
        return

    community_rows = [
        {"normalized_name": normalize_entity_name(name), "community": int(community)}
        for name, community in enrichment.entity_communities.items()
        if normalize_entity_name(name)
    ]
    call_rows = [
        {
            "src": normalize_entity_name(src),
            "dst": normalize_entity_name(dst),
            "source_file": source_file or "",
            "source_location": source_location or "",
        }
        for src, dst, source_file, source_location in enrichment.call_edges
        if normalize_entity_name(src)
        and normalize_entity_name(dst)
        and normalize_entity_name(src) != normalize_entity_name(dst)
    ]
    community_label_rows = [
        {"community": int(cid), "label": label}
        for cid, label in enrichment.community_labels.items()
        if label
    ]

    if not (community_rows or call_rows or community_label_rows):
        return

    async with driver.session() as session:
        for row_batch in _row_batches(
            community_rows,
            batch_size=GRAPH_WRITE_ROW_BATCH_SIZE,
        ):
            # Stamp community ID on entities. We match on normalized_name so
            # graphify's "Combat.PunchAttack" finds the Phase-4-written entity
            # that has canonical_name="combatpunchattack" — the existing
            # canonicalization rule lossy-strips dots/punctuation.
            await session.run(
                """
                UNWIND $rows AS row
                MATCH (e:Entity {normalized_name: row.normalized_name})
                SET e.graphify_community = row.community
                """,
                rows=row_batch,
            )

        for row_batch in _row_batches(
            community_label_rows,
            batch_size=GRAPH_WRITE_ROW_BATCH_SIZE,
        ):
            await session.run(
                """
                UNWIND $rows AS row
                MATCH (e:Entity {graphify_community: row.community})
                SET e.graphify_community_label = row.label
                """,
                rows=row_batch,
            )

        for row_batch in _row_batches(
            call_rows,
            batch_size=GRAPH_WRITE_ROW_BATCH_SIZE,
        ):
            # CALLS edges between Entity nodes. MATCH by normalized_name on
            # both endpoints; only create the edge if BOTH entities already
            # exist (Phase 4 should have created them from symbols_defined).
            #
            # Storage shape matches RELATES_TO above (corpus_ids as array,
            # not a single corpus_id string). This is the convention every
            # retriever-side WHERE clause uses (see neo4j_reader.py:173,335:
            # `WHERE any(cid IN $corpus_ids WHERE cid IN coalesce(r.corpus_ids, []))`).
            # Using the array shape means CALLS edges become discoverable to
            # corpus-scoped retrieval without bespoke filter logic.
            await session.run(
                """
                UNWIND $rows AS row
                MATCH (src:Entity {normalized_name: row.src})
                MATCH (dst:Entity {normalized_name: row.dst})
                MERGE (src)-[r:CALLS]->(dst)
                ON CREATE SET r.confidence = 1.0,
                              r.extractor = 'graphify',
                              r.first_seen = timestamp()
                SET r.source_file = coalesce(row.source_file, r.source_file),
                    r.source_location = coalesce(row.source_location, r.source_location),
                    r.corpus_ids = CASE
                        WHEN r.corpus_ids IS NULL THEN [$corpus_id]
                        WHEN $corpus_id IN r.corpus_ids THEN r.corpus_ids
                        ELSE r.corpus_ids + [$corpus_id]
                    END
                """,
                rows=row_batch,
                corpus_id=corpus_id,
            )

    logger.info(
        "Phase 4.5 graphify enrichment written: corpus=%s communities=%d "
        "community_labels=%d call_edges=%d",
        corpus_id,
        len(community_rows),
        len(community_label_rows),
        len(call_rows),
    )

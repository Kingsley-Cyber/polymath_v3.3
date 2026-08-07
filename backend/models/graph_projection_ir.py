"""GraphProjectionIR — typed, deterministic Neo4j projection contract.

Ontology remains outside Neo4j. This IR is built from Mongo extraction
artifacts and consumed only by the graph projector.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

GRAPH_PROJECTION_IR_VERSION = "polymath.graph_projection_ir.v1"

AuthorityClass = Literal[
    "structural",
    "extracted_entity",
    "extracted_assertion",
    "qualified_fact",
]

ProjectionLane = Literal[
    "structural",
    "entity_mentions",
    "relation_assertions",
    "qualified_facts",
]


class GraphEntityNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=1)
    corpus_id: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    entity_type: str = ""
    trusted_aliases: tuple[str, ...] = ()
    authority: AuthorityClass = "extracted_entity"
    ontology_class_id: str = ""
    ontology_release: str = ""


class GraphMentionEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    entity_id: str
    corpus_id: str
    confidence: float = 0.0
    evidence_text: str = ""
    authority: AuthorityClass = "structural"


class GraphAssertionNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_id: str = Field(min_length=1)
    corpus_id: str = Field(min_length=1)
    subject_entity_id: str = Field(min_length=1)
    predicate_id: str = Field(min_length=1)
    object_entity_id: str = Field(min_length=1)
    source_document_id: str = ""
    source_chunk_id: str = ""
    evidence_text: str = ""
    confidence: float = 0.0
    acceptance_lane: str = ""
    negated: bool = False
    modal: str = ""
    assertion_status: str = "accepted"
    extractor_release: str = ""
    model_hash: str = ""
    ontology_release: str = ""
    acceptance_policy_release: str = ""
    contract_hash: str = ""
    authority: AuthorityClass = "extracted_assertion"
    canonical: bool = False


class GraphAssertionSupportEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    assertion_id: str
    corpus_id: str


class GraphProjectionIR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = GRAPH_PROJECTION_IR_VERSION
    corpus_id: str
    document_id: str
    ontology_release: str = ""
    projection_release: str = "polymath.graph_projection.v1"
    extraction_release: str = ""
    entity_nodes: tuple[GraphEntityNode, ...] = ()
    mention_edges: tuple[GraphMentionEdge, ...] = ()
    assertion_nodes: tuple[GraphAssertionNode, ...] = ()
    assertion_support_edges: tuple[GraphAssertionSupportEdge, ...] = ()
    unresolved_entities: tuple[str, ...] = ()
    unresolved_predicates: tuple[str, ...] = ()
    rejected_assertions: tuple[str, ...] = ()
    expected_counts: dict[str, int] = Field(default_factory=dict)

    def projection_hash(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def expected_counts_computed(self) -> dict[str, int]:
        return {
            "entity_nodes": len(self.entity_nodes),
            "mention_edges": len(self.mention_edges),
            "assertion_nodes": len(self.assertion_nodes),
            "assertion_support_edges": len(self.assertion_support_edges),
        }


def deterministic_assertion_id(
    *,
    corpus_id: str,
    chunk_id: str,
    subject_entity_id: str,
    predicate_id: str,
    object_entity_id: str,
) -> str:
    raw = "|".join(
        [
            str(corpus_id),
            str(chunk_id),
            str(subject_entity_id),
            str(predicate_id),
            str(object_entity_id),
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"assertion:{digest}"


def graph_capabilities_template(**kwargs: Any) -> dict[str, Any]:
    base = {
        "extraction_terminal": False,
        "structural_ready": False,
        "entity_ready": False,
        "assertion_ready": False,
        "qualified_fact_ready": False,
        "advertised_mode": "blocked",
        "reason": "",
        "counts": {
            "document_nodes": 0,
            "chunk_nodes": 0,
            "entity_nodes": 0,
            "assertion_nodes": 0,
            "fact_nodes": 0,
        },
    }
    base.update(kwargs)
    return base

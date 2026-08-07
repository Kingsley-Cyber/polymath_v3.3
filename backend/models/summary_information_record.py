"""SummaryInformationRecordV1 — post-gate inputs for deterministic summaries.

Summaries may route/orient; they must never create alias identity, graph facts,
or answer citations.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SUMMARY_INFORMATION_RECORD_VERSION = "summary_information_record.v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class SummaryInformationRecordV1(StrictModel):
    schema_version: Literal["summary_information_record.v1"] = (
        SUMMARY_INFORMATION_RECORD_VERSION
    )
    source_child_id: str
    parent_id: str = ""
    section_id: str = ""
    document_id: str
    corpus_id: str = ""
    heading_path: list[str] = Field(default_factory=list)
    primary_entities: list[str] = Field(default_factory=list)
    trusted_aliases: list[str] = Field(default_factory=list)
    definitions: list[str] = Field(default_factory=list)
    accepted_relation_assertions: list[dict[str, Any]] = Field(default_factory=list)
    qualified_claims: list[dict[str, Any]] = Field(default_factory=list)
    qualified_facts: list[dict[str, Any]] = Field(default_factory=list)
    dates: list[str] = Field(default_factory=list)
    quantities: list[str] = Field(default_factory=list)
    negation: list[dict[str, Any]] = Field(default_factory=list)
    modality: list[dict[str, Any]] = Field(default_factory=list)
    representative_source_sentences: list[str] = Field(default_factory=list)
    source_evidence_ids: list[str] = Field(default_factory=list)
    source_child_ids: list[str] = Field(default_factory=list)
    input_hash: str = ""
    # Authority flags (frozen)
    may_route_retrieval: bool = True
    may_orient_llm_context: bool = True
    may_create_alias_identity: bool = False
    may_create_graph_facts: bool = False
    may_be_answer_citation: bool = False

    def compute_input_hash(self) -> str:
        payload = self.model_dump(exclude={"input_hash"})
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return f"sha256:{digest}"

    def with_hash(self) -> "SummaryInformationRecordV1":
        return self.model_copy(update={"input_hash": self.compute_input_hash()})


class AggregatedSummaryRecordV1(StrictModel):
    """Parent / section / document aggregation over child SummaryInformationRecords."""

    schema_version: Literal["aggregated_summary_record.v1"] = "aggregated_summary_record.v1"
    level: Literal["parent", "section", "document"]
    corpus_id: str
    document_id: str
    parent_id: str = ""
    section_id: str = ""
    source_child_ids: list[str] = Field(default_factory=list)
    primary_entities: list[str] = Field(default_factory=list)
    trusted_aliases: list[str] = Field(default_factory=list)
    accepted_relation_assertions: list[dict[str, Any]] = Field(default_factory=list)
    representative_source_sentences: list[str] = Field(default_factory=list)
    child_input_hashes: list[str] = Field(default_factory=list)
    aggregation_hash: str = ""
    may_route_retrieval: bool = True
    may_be_answer_citation: bool = False
    may_create_alias_identity: bool = False
    may_create_graph_facts: bool = False

    def with_hash(self) -> "AggregatedSummaryRecordV1":
        payload = self.model_dump(exclude={"aggregation_hash"})
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        return self.model_copy(update={"aggregation_hash": f"sha256:{digest}"})

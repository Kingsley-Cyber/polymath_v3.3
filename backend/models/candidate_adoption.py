"""Candidate-adoption comparison contracts — dark integration only."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


def _hash_payload(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"sha256:{digest}"


class CandidateAdoptionComparisonV1(StrictModel):
    schema_version: Literal["candidate_adoption_comparison.v1"] = (
        "candidate_adoption_comparison.v1"
    )
    query_id: str = Field(min_length=1)
    corpus_ids: list[str] = Field(default_factory=list)
    user_id: str = ""
    query_class: str = ""
    baseline_finalist_ids: list[str] = Field(default_factory=list)
    candidate_finalist_ids: list[str] = Field(default_factory=list)
    graph_added_candidate_ids: list[str] = Field(default_factory=list)
    graph_added_finalist_ids: list[str] = Field(default_factory=list)
    cq_winner_ids: list[str] = Field(default_factory=list)
    cq_winners_in_candidate_finalists: list[str] = Field(default_factory=list)
    baseline_obligation_coverage: float = 0.0
    candidate_obligation_coverage: float = 0.0
    baseline_context_hash: str = ""
    candidate_context_hash: str = ""
    ranking_mutated_inside_scope: bool = False
    ranking_mutated_outside_scope: bool = False
    user_visible_answer_mutated: bool = False
    graph_execution_status: str = "not_required"
    duplicate_root_embeddings: int = 0
    duplicate_wave1_searches: int = 0
    hydration_batches: int = 0
    reranker_calls: int = 0
    neo4j_round_trips: int = 0
    baseline_retrieval_ms: float = 0.0
    candidate_retrieval_ms: float = 0.0
    candidate_synthesis_ms: float = 0.0
    candidate_answer_chars: int = 0
    verification_status: str = "not_run"
    claims_total: int = 0
    claims_supported: int = 0
    claims_unsupported: int = 0
    citations_total: int = 0
    citations_resolved: int = 0
    rollback_triggered: bool = False
    rollback_reasons: list[str] = Field(default_factory=list)
    result_hash: str = ""

    def with_hash(self) -> "CandidateAdoptionComparisonV1":
        payload = self.model_dump(exclude={"result_hash"})
        return self.model_copy(update={"result_hash": _hash_payload(payload)})

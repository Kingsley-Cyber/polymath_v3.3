"""Dark-canary comparison contracts — shadow-only, never mutate answers."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

GraphStatus = Literal[
    "executed_with_paths",
    "not_required",
    "no_supported_path",
    "partial_graph",
    "blocked",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


def _hash_payload(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"sha256:{digest}"


class DarkCanaryComparisonV1(StrictModel):
    schema_version: Literal["dark_canary_comparison.v1"] = "dark_canary_comparison.v1"
    query_id: str = Field(min_length=1)
    corpus_ids: list[str] = Field(default_factory=list)
    user_id: str = ""
    query_class: str = ""
    requested_route: str = ""
    effective_route: str = ""
    graph_status: GraphStatus = "not_required"
    downgrade_reason: str = ""
    baseline_child_ids: list[str] = Field(default_factory=list)
    dark_child_ids: list[str] = Field(default_factory=list)
    graph_added_child_ids: list[str] = Field(default_factory=list)
    baseline_obligation_coverage: float = 0.0
    dark_obligation_coverage: float = 0.0
    baseline_citation_validity: float = 1.0
    dark_citation_validity: float = 1.0
    graph_paths_used: int = 0
    every_path_has_child_support: bool = True
    unsupported_selected_paths: int = 0
    false_transitive_inferences: int = 0
    ambiguous_identity_merges: int = 0
    unexplained_empty_graph_paths: int = 0
    irrelevant_graph_addition: bool = False
    baseline_retrieval_ms: float = 0.0
    dark_retrieval_ms: float = 0.0
    retrieval_ms: float = 0.0
    dark_synthesis_ms: float = 0.0
    dark_synthesis_ran: bool = False
    neo4j_round_trips: int = 0
    hydration_batches: int = 0
    reranker_calls: int = 0
    claims_total: int = 0
    claims_supported: int = 0
    claims_unsupported: int = 0
    packet_claims_unsupported: int = 0
    contradiction_disclosed: bool = False
    temporal_selection_correct: bool | None = None
    silent_hybrid_fallback: int = 0
    synthesis_preflight_status: str = "unknown"
    provider_status: str = "not_run"
    production_answer_mutations: int = 0
    production_ranking_mutated: bool = False
    user_visible_answer_changed: bool = False
    rollback_triggered: bool = False
    rollback_reasons: list[str] = Field(default_factory=list)
    result_hash: str = ""

    def with_hash(self) -> "DarkCanaryComparisonV1":
        payload = self.model_dump(exclude={"result_hash"})
        return self.model_copy(update={"result_hash": _hash_payload(payload)})

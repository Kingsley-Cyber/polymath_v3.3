"""Typed Query IR contract — validates and extends QueryPlanV2 (Slice 1).

Owner decision 2026-08-02: the Query IR is NOT a second planner. The
authoritative deterministic planner stays ``build_query_plan_v2`` →
``QueryPlanV2`` (services/retriever/query_plan.py). This contract carries
the plan's fields by value plus the typed stages the target pipeline needs:

    QueryPlanV2 fields (mirrored)
    + TemporalCompilation   explicit clock interpretation
    + OntologyRef[]         canonical entity/predicate refs (advisory only)
    + EvidenceObligation[]  what must be retrieved before synthesis
    + RoutePolicy           FAST / CURATED / RESEARCH classification
    + release_pins          version stamps for every trace

Invariants encoded here:
- ``original_query`` is preserved verbatim (query-planning invariant).
- Ontology refs are advisory: they shape routing, never evidence.
- No unvalidated Query IR may reach a retriever (gate added in step 5/6 of
  the owner-mandated order; Slice 1 ships the contract + tests only).

Dependency-light by design: this module must not import services.* so the
contract tests run without the backend stack. ``from_plan`` duck-types the
QueryPlanV2 shape.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from models.release_stamp import ReleaseStamp

QUERY_IR_SCHEMA_VERSION = "polymath.query_ir.v1"

ClockInterpretation = Literal[
    "none",
    "now_relative",
    "document_relative",
    "absolute",
]

RouteName = Literal["FAST", "CURATED", "RESEARCH"]


class TemporalCompilation(BaseModel):
    """Explicit clock interpretation for temporal queries.

    Invariant: no temporal query runs without an explicit clock
    interpretation. Inactive detections still emit ``interpretation="none"``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    active: bool = False
    interpretation: ClockInterpretation = "none"
    detector_version: str = ""
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class OntologyRef(BaseModel):
    """Canonical entity/predicate/class reference resolved from vocabulary.

    Advisory only — must never enter RetrievalPayload or mutate scores.
    ``applicability`` inherits the vocabulary tier (direct vs exploratory).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref_kind: Literal["entity", "predicate", "class", "role"]
    canonical_key: str = Field(min_length=1)
    surface: str = ""
    applicability: Literal["direct", "source_term_overlap", "associative"] = (
        "associative"
    )
    source_lexicon_id: str = ""
    release_pin: str = ""


class EvidenceObligation(BaseModel):
    """One independently meaningful answer obligation of the plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    obligation_id: str = Field(min_length=1)
    lane_ids: tuple[str, ...] = ()
    description: str = ""
    required: bool = True


class RoutePolicy(BaseModel):
    """Deterministic route classification (never an LLM decision)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    route: RouteName = "CURATED"
    reason: str = ""


class RequiredDomain(BaseModel):
    """One required domain / answer neighborhood for cross-domain curation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    domain_id: str = Field(min_length=1)
    obligation: str = ""
    required: bool = True


class BridgeObligation(BaseModel):
    """Evidence-supported mapping that must retain two-sided child support."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    bridge_id: str = Field(min_length=1)
    source_domain: str = ""
    target_domain: str = ""
    description: str = ""
    required: bool = True


class QueryIRLane(BaseModel):
    """Value-copy of the QueryPlanV2 lane fields the IR must preserve."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane_id: str = Field(min_length=1)
    role: str
    query: str
    required: bool = True


class QueryIR(BaseModel):
    """Validated intermediate representation reaching the retriever."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default=QUERY_IR_SCHEMA_VERSION)

    # ── QueryPlanV2 fields mirrored by value ────────────────────────────────
    plan_version: str = Field(min_length=1)
    original_query: str = Field(min_length=1)
    standalone_query: str = Field(min_length=1)
    complexity: str = Field(min_length=1)
    concepts: tuple[str, ...] = ()
    operators: tuple[str, ...] = ()
    lanes: tuple[QueryIRLane, ...] = ()
    answer_shape: str = "single_fact"
    corpus_ids: tuple[str, ...] = ()
    # Cross-domain directive fields (additive; empty = single-domain / unset).
    selected_corpus_ids: tuple[str, ...] = ()
    conversation_resolution_hash: str = ""
    requested_retrieval_mode: str = ""
    required_domains: tuple[RequiredDomain, ...] = ()
    bridge_obligations: tuple[BridgeObligation, ...] = ()
    evidence_token_budget: int = 7000
    minimum_evidence_quality: float = 0.0

    # ── typed IR stages ─────────────────────────────────────────────────────
    temporal: TemporalCompilation = Field(default_factory=TemporalCompilation)
    ontology_refs: tuple[OntologyRef, ...] = ()
    obligations: tuple[EvidenceObligation, ...] = ()
    route_policy: RoutePolicy = Field(default_factory=RoutePolicy)
    #: POLICY context — the desired/required release pins this query run is
    #: expected to satisfy (scope prior for later gating slices).
    release_pins: dict[str, str] = Field(default_factory=dict)
    #: RECORDED identity — the release identity present when this query
    #: artifact was created. Distinct from ``release_pins`` (policy) and from
    #: ``diagnostics.release_stamps_read`` (identities actually observed on
    #: retrieved evidence, emitted by the retriever, not carried here).
    #: Owner-frozen rule: the three concepts must never be merged or allowed
    #: to overwrite one another. Descriptive only, nullable when unknown.
    release_stamp: ReleaseStamp | None = None

    @field_validator("original_query")
    @classmethod
    def _original_query_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("original_query must preserve the user's message")
        return value

    @field_validator("lanes")
    @classmethod
    def _lane_ids_unique(cls, lanes: tuple[QueryIRLane, ...]) -> tuple[QueryIRLane, ...]:
        ids = [lane.lane_id for lane in lanes]
        if len(ids) != len(set(ids)):
            raise ValueError("lane_id values must be unique")
        return lanes

    @classmethod
    def from_plan(
        cls,
        plan: Any,
        *,
        temporal: TemporalCompilation | None = None,
        ontology_refs: tuple[OntologyRef, ...] = (),
        obligations: tuple[EvidenceObligation, ...] | None = None,
        route_policy: RoutePolicy | None = None,
        release_pins: dict[str, str] | None = None,
        release_stamp: ReleaseStamp | None = None,
        required_domains: tuple[RequiredDomain, ...] = (),
        bridge_obligations: tuple[BridgeObligation, ...] = (),
        conversation_resolution_hash: str = "",
        requested_retrieval_mode: str = "",
        evidence_token_budget: int | None = None,
        minimum_evidence_quality: float | None = None,
        selected_corpus_ids: tuple[str, ...] | None = None,
    ) -> "QueryIR":
        """Build the IR from a duck-typed QueryPlanV2.

        Deliberately duck-typed so this module never imports services.* —
        the planner remains authoritative and this contract stays testable
        without the backend stack.
        """

        lanes = tuple(
            QueryIRLane(
                lane_id=lane.lane_id,
                role=lane.role,
                query=lane.query,
                required=lane.required,
            )
            for lane in getattr(plan, "lanes", ())
        )
        if obligations is None:
            obligations = tuple(
                EvidenceObligation(
                    obligation_id=lane.lane_id,
                    lane_ids=(lane.lane_id,),
                    description=lane.query,
                    required=lane.required,
                )
                for lane in lanes
                if lane.required
            )
        corpus_ids = tuple(getattr(plan, "corpus_ids", ()))
        return cls(
            plan_version=getattr(plan, "version", ""),
            original_query=getattr(plan, "original_query", ""),
            standalone_query=getattr(plan, "standalone_query", ""),
            complexity=getattr(plan, "complexity", ""),
            concepts=tuple(getattr(plan, "concepts", ())),
            operators=tuple(getattr(plan, "operators", ())),
            lanes=lanes,
            answer_shape=getattr(plan, "answer_shape", "single_fact"),
            corpus_ids=corpus_ids,
            selected_corpus_ids=(
                tuple(selected_corpus_ids)
                if selected_corpus_ids is not None
                else corpus_ids
            ),
            conversation_resolution_hash=str(conversation_resolution_hash or ""),
            requested_retrieval_mode=str(requested_retrieval_mode or ""),
            required_domains=tuple(required_domains or ()),
            bridge_obligations=tuple(bridge_obligations or ()),
            evidence_token_budget=(
                int(evidence_token_budget)
                if evidence_token_budget is not None
                else 7000
            ),
            minimum_evidence_quality=(
                float(minimum_evidence_quality)
                if minimum_evidence_quality is not None
                else 0.0
            ),
            temporal=temporal or TemporalCompilation(),
            ontology_refs=ontology_refs,
            obligations=obligations,
            route_policy=route_policy or RoutePolicy(),
            release_pins=dict(release_pins or {}),
            release_stamp=release_stamp,
        )

    def ir_hash(self) -> str:
        """Deterministic identity: same IR fields ⇒ same hash (replayability)."""

        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

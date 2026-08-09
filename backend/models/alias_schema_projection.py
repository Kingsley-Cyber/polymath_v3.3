"""Shadow schema projection + schema-assisted retrieval contracts (Phase 7).

Never overwrite production schemas. Trust classes stay separated.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from models.alias_identity import (
    StrictModel,
    alias_contract_hash,
)

SHADOW_SCHEMA_PROJECTION_RELEASE = "shadow_schemas_projection.v1"
ALIAS_SCHEMA_RETRIEVAL_RELEASE = "alias_schema_retrieval.v1"

TrustClass = Literal[
    "trusted_aliases",
    "retrieval_surface_variants",
    "ambiguous_aliases",
    "related_terms",
    "descriptions",
    "temporal_aliases",
    "legacy_unqualified",
]

ExpansionMode = Literal[
    "canonical_query_expansion",
    "bounded_assistance",
    "scoped_only",
    "shadow_trace_only",
    "metadata_only",
    "none",
]

RankingContribution = Literal[
    "authoritative_expansion",
    "bounded_assistance",
    "scoped_assistance",
    "trace_only_no_ranking",
    "none",
]


class ShadowSchemaSurfaceV1(StrictModel):
    surface: str = Field(min_length=1)
    trust_class: TrustClass
    identity_authority: bool
    expansion_mode: ExpansionMode
    qualification: str = "alias_pipeline_v1"
    source_alias_candidate_ids: list[str] = Field(default_factory=list)
    source_alias_decision_ids: list[str] = Field(default_factory=list)
    expansion_scope: str | None = None  # e.g. document_or_parent_only


class ShadowSchemaRecordV1(StrictModel):
    """One shadow schema point — isolated from production corpus_*_schemas."""

    schema_point_id: str = Field(min_length=1)
    corpus_id: str = Field(min_length=1)
    corpus_entity_id: str = Field(min_length=1)
    canonical_term: str = Field(min_length=1)
    entity_type: str | None = None
    trusted_aliases: list[ShadowSchemaSurfaceV1]
    temporal_aliases: list[ShadowSchemaSurfaceV1]
    retrieval_surface_variants: list[ShadowSchemaSurfaceV1]
    ambiguous_aliases: list[ShadowSchemaSurfaceV1]
    related_terms: list[ShadowSchemaSurfaceV1]
    descriptions: list[ShadowSchemaSurfaceV1]
    legacy_unqualified: list[ShadowSchemaSurfaceV1]
    linked_child_ids: list[str]
    linked_parent_ids: list[str]
    linked_section_ids: list[str]
    linked_graph_node_ids: list[str]
    source_document_ids: list[str]
    supporting_alias_decision_ids: list[str]
    projection_release: str = Field(min_length=1)
    contract_hash: str = Field(min_length=1)
    # Hard marker — writers must refuse production collections.
    shadow_only: bool = True
    identity_authority_default: bool = False

    @model_validator(mode="after")
    def validate_shadow_invariants(self) -> "ShadowSchemaRecordV1":
        if not self.shadow_only:
            raise ValueError("Phase-7 projections must remain shadow_only=true")
        # Descriptions must never appear inside alias trust classes.
        for bucket in (
            self.trusted_aliases,
            self.temporal_aliases,
            self.retrieval_surface_variants,
            self.ambiguous_aliases,
        ):
            for surf in bucket:
                if surf.trust_class == "descriptions":
                    raise ValueError("descriptions cannot enter alias trust classes")
        for surf in self.trusted_aliases:
            if not surf.identity_authority:
                raise ValueError("trusted_aliases require identity_authority=true")
            if surf.expansion_mode != "canonical_query_expansion":
                raise ValueError("trusted_aliases require canonical_query_expansion")
        for surf in self.retrieval_surface_variants:
            if surf.identity_authority:
                raise ValueError("retrieval variants forbid identity_authority")
        for surf in self.ambiguous_aliases:
            if surf.identity_authority:
                raise ValueError("ambiguous aliases forbid identity_authority")
            if surf.expansion_mode != "scoped_only":
                raise ValueError("ambiguous aliases require scoped_only expansion")
        for surf in self.related_terms:
            if surf.expansion_mode != "shadow_trace_only":
                raise ValueError("related_terms must be shadow_trace_only")
            if surf.identity_authority:
                raise ValueError("related_terms forbid identity_authority")
        for surf in self.descriptions:
            if surf.expansion_mode != "metadata_only":
                raise ValueError("descriptions must be metadata_only")
        expected = alias_contract_hash(self.model_dump(exclude={"contract_hash"}))
        if self.contract_hash != expected:
            raise ValueError("contract_hash must match deterministic payload hash")
        return self

    @classmethod
    def create(cls, **kwargs) -> "ShadowSchemaRecordV1":
        draft = dict(kwargs)
        draft.setdefault("projection_release", SHADOW_SCHEMA_PROJECTION_RELEASE)
        draft.setdefault("shadow_only", True)
        draft.setdefault("identity_authority_default", False)

        def _as_surface(row: object) -> ShadowSchemaSurfaceV1:
            if isinstance(row, ShadowSchemaSurfaceV1):
                return row
            if isinstance(row, dict):
                return ShadowSchemaSurfaceV1(**row)
            raise TypeError("surface rows must be ShadowSchemaSurfaceV1 or dict")

        for key in (
            "trusted_aliases",
            "temporal_aliases",
            "retrieval_surface_variants",
            "ambiguous_aliases",
            "related_terms",
            "descriptions",
            "legacy_unqualified",
        ):
            rows = [_as_surface(row) for row in (draft.get(key) or [])]
            draft[key] = sorted(
                rows, key=lambda s: (s.surface.lower(), s.trust_class)
            )
        for key in (
            "linked_child_ids",
            "linked_parent_ids",
            "linked_section_ids",
            "linked_graph_node_ids",
            "source_document_ids",
            "supporting_alias_decision_ids",
        ):
            draft[key] = sorted(set(draft.get(key) or []))

        schema_point_id = (
            "shadowschema:"
            + alias_contract_hash(
                {
                    "corpus_id": draft["corpus_id"],
                    "corpus_entity_id": draft["corpus_entity_id"],
                    "canonical_term": " ".join(
                        str(draft["canonical_term"]).lower().split()
                    ),
                    "projection_release": draft["projection_release"],
                }
            )
        )
        draft["schema_point_id"] = schema_point_id
        serializable = {
            **draft,
            **{
                key: [s.model_dump() for s in draft[key]]
                for key in (
                    "trusted_aliases",
                    "temporal_aliases",
                    "retrieval_surface_variants",
                    "ambiguous_aliases",
                    "related_terms",
                    "descriptions",
                    "legacy_unqualified",
                )
            },
        }
        contract_hash = alias_contract_hash(serializable)
        return cls(**draft, contract_hash=contract_hash)


class SchemaAssistedTraceV1(StrictModel):
    """Required provenance for every schema-assisted retrieval."""

    query: str = Field(min_length=1)
    schema_point_id: str | None = None
    corpus_entity_id: str | None = None
    alias_candidate_id: str | None = None
    alias_decision_id: str | None = None
    matched_surface: str | None = None
    canonical_term: str | None = None
    trust_class: TrustClass | None = None
    linked_child_ids: list[str] = Field(default_factory=list)
    linked_parent_ids: list[str] = Field(default_factory=list)
    linked_section_ids: list[str] = Field(default_factory=list)
    expanded_query: str | None = None
    ranking_contribution: RankingContribution = "none"
    final_hydrated_child_ids: list[str] = Field(default_factory=list)
    original_query_lane_ran: bool = True
    schema_used_as_answer_evidence: bool = False
    retrieval_release: str = ALIAS_SCHEMA_RETRIEVAL_RELEASE
    trace_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_trace(self) -> "SchemaAssistedTraceV1":
        if not self.original_query_lane_ran:
            raise ValueError("original_query_lane must always run")
        if self.schema_used_as_answer_evidence:
            raise ValueError("schema records must never be answer evidence")
        if self.ranking_contribution == "trace_only_no_ranking" and self.trust_class not in {
            "related_terms",
            None,
        }:
            # related_terms are the primary shadow-only class; allow also when
            # no schema hit occurred.
            pass
        expected = alias_contract_hash(self.model_dump(exclude={"trace_hash"}))
        if self.trace_hash != expected:
            raise ValueError("trace_hash must match deterministic payload hash")
        return self

    @classmethod
    def create(cls, **kwargs) -> "SchemaAssistedTraceV1":
        draft = dict(kwargs)
        draft.setdefault("retrieval_release", ALIAS_SCHEMA_RETRIEVAL_RELEASE)
        draft.setdefault("original_query_lane_ran", True)
        draft.setdefault("schema_used_as_answer_evidence", False)
        draft.setdefault("ranking_contribution", "none")
        for key in (
            "linked_child_ids",
            "linked_parent_ids",
            "linked_section_ids",
            "final_hydrated_child_ids",
        ):
            draft[key] = sorted(set(draft.get(key) or []))
        # Hash the same payload shape the model validator sees (model_dump).
        probe = cls.model_construct(**draft, trace_hash="pending")
        payload = probe.model_dump(exclude={"trace_hash"})
        return cls(**draft, trace_hash=alias_contract_hash(payload))

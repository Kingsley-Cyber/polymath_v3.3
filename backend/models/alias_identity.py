"""Deterministic alias identity contracts (Alias Pipeline directive v1.0).

Observation-first: these models validate and hash alias candidates / decisions /
document+corpus entities. They do not authorize production schema switches or
corpus backfills. Fail closed when required provenance is missing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.semantic_artifacts import domain_hash


ALIAS_PIPELINE_RELEASE = "deterministic_alias_pipeline.v1"
ALIAS_GATE_RELEASE = "alias_gate.v1"
ALIAS_CLUSTER_RELEASE = "alias_cluster.v1"
PARENT_ALIAS_BUNDLE_RELEASE = "parent_alias_aggregation.v1"
CORPUS_MERGE_RELEASE = "corpus_merge.v1"
CORPUS_CLUSTER_RELEASE = "corpus_cluster.v1"
ALIAS_CONTRACT_HASH_NAMESPACE = "alias_identity.v1"

AliasCandidateType = Literal[
    "explicit_alternate_name",
    "explicit_alias_pattern",
    "acronym_long_form",
    "explicit_abbreviation",
    "former_name",
    "curated_alias",
    "proper_name_apposition",
    "casing_variant",
    "punctuation_variant",
    "morphological_variant",
    "extraction_surface_variant",
    "relex_synonym_relation",
    "semantic_related_term",
    "descriptive_apposition",
    "role_apposition",
    "location_apposition",
]

AliasScope = Literal["mention", "sentence", "parent", "document", "corpus"]

AliasDecisionKind = Literal[
    "ACCEPT_IDENTITY",
    "ACCEPT_TEMPORAL_IDENTITY",
    "ACCEPT_RETRIEVAL_ONLY",
    "REVIEW",
    "REJECT",
]

AmbiguityStatus = Literal[
    "unambiguous",
    "unambiguous_within_parent",
    "ambiguous_acronym",
    "ambiguous_surface",
    "type_conflict",
    "insufficient_evidence",
    "not_applicable",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, protected_namespaces=())


class IncompleteAliasCandidate(StrictModel):
    """Provenance-incomplete alias observation (fail-closed relative to V1).

    Never treat as ACCEPT_IDENTITY input. Legacy query_aliases may still carry
    the surface string for retrieval compatibility.
    """

    status: Literal["incomplete"] = "incomplete"
    incomplete_reason: str = Field(min_length=1)
    candidate_type: AliasCandidateType | None = None
    source_method: str = Field(min_length=1)
    rule_id: str | None = None
    rule_release: str | None = None
    canonical_surface: str = ""
    candidate_surface: str = ""
    document_id: str = ""
    chunk_id: str = ""
    sentence_id: str | None = None
    canonical_start: int | None = None
    canonical_end: int | None = None
    candidate_start: int | None = None
    candidate_end: int | None = None
    evidence_text: str = ""
    scope: AliasScope | None = None
    confidence: float | None = None
    extractor_release: str = ALIAS_PIPELINE_RELEASE


def _require_positive_span(start: int, end: int, *, label: str) -> None:
    if start < 0 or end <= start:
        raise ValueError(f"{label} must form a positive source span")


def alias_contract_hash(payload: dict) -> str:
    """Stable sha256 over canonicalized payload (directive contract_hash)."""

    return domain_hash(ALIAS_CONTRACT_HASH_NAMESPACE, payload)


def build_alias_candidate_id(
    *,
    candidate_type: str,
    rule_id: str,
    document_id: str,
    chunk_id: str,
    sentence_id: str,
    canonical_start: int,
    canonical_end: int,
    candidate_start: int,
    candidate_end: int,
    canonical_surface: str,
    candidate_surface: str,
) -> str:
    """Deterministic id from normalized type/rule/ids/spans/surfaces."""

    digest = alias_contract_hash(
        {
            "candidate_type": candidate_type,
            "rule_id": rule_id,
            "document_id": document_id,
            "chunk_id": chunk_id,
            "sentence_id": sentence_id,
            "canonical_start": canonical_start,
            "canonical_end": canonical_end,
            "candidate_start": candidate_start,
            "candidate_end": candidate_end,
            "canonical_surface": " ".join(canonical_surface.lower().split()),
            "candidate_surface": " ".join(candidate_surface.lower().split()),
        }
    )
    return f"aliascand:{digest}"


class AliasCandidateV1(StrictModel):
    alias_candidate_id: str
    candidate_type: AliasCandidateType
    source_method: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    rule_release: str = Field(min_length=1)
    canonical_surface: str = Field(min_length=1)
    candidate_surface: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    chunk_id: str = Field(min_length=1)
    sentence_id: str = Field(min_length=1)
    canonical_start: int
    canonical_end: int
    candidate_start: int
    candidate_end: int
    evidence_text: str = Field(min_length=1)
    entity_type: str | None = None
    candidate_entity_type: str | None = None
    scope: AliasScope
    confidence: float = Field(ge=0.0, le=1.0)
    contract_hash: str = Field(min_length=1)
    extractor_release: str = Field(min_length=1)
    model_release: str | None = None

    @model_validator(mode="after")
    def validate_provenance(self) -> "AliasCandidateV1":
        _require_positive_span(
            self.canonical_start, self.canonical_end, label="canonical span"
        )
        _require_positive_span(
            self.candidate_start, self.candidate_end, label="candidate span"
        )
        expected_id = build_alias_candidate_id(
            candidate_type=self.candidate_type,
            rule_id=self.rule_id,
            document_id=self.document_id,
            chunk_id=self.chunk_id,
            sentence_id=self.sentence_id,
            canonical_start=self.canonical_start,
            canonical_end=self.canonical_end,
            candidate_start=self.candidate_start,
            candidate_end=self.candidate_end,
            canonical_surface=self.canonical_surface,
            candidate_surface=self.candidate_surface,
        )
        if self.alias_candidate_id != expected_id:
            raise ValueError(
                "alias_candidate_id must match deterministic id rule "
                f"(expected {expected_id})"
            )
        expected_hash = alias_contract_hash(
            self.model_dump(exclude={"contract_hash"})
        )
        if self.contract_hash != expected_hash:
            raise ValueError("contract_hash must match deterministic payload hash")
        return self

    @classmethod
    def create(
        cls,
        *,
        candidate_type: AliasCandidateType,
        source_method: str,
        rule_id: str,
        rule_release: str,
        canonical_surface: str,
        candidate_surface: str,
        document_id: str,
        chunk_id: str,
        sentence_id: str,
        canonical_start: int,
        canonical_end: int,
        candidate_start: int,
        candidate_end: int,
        evidence_text: str,
        scope: AliasScope,
        confidence: float,
        extractor_release: str = ALIAS_PIPELINE_RELEASE,
        entity_type: str | None = None,
        candidate_entity_type: str | None = None,
        model_release: str | None = None,
    ) -> "AliasCandidateV1":
        """Build a valid candidate with deterministic id + contract_hash."""

        alias_candidate_id = build_alias_candidate_id(
            candidate_type=candidate_type,
            rule_id=rule_id,
            document_id=document_id,
            chunk_id=chunk_id,
            sentence_id=sentence_id,
            canonical_start=canonical_start,
            canonical_end=canonical_end,
            candidate_start=candidate_start,
            candidate_end=candidate_end,
            canonical_surface=canonical_surface,
            candidate_surface=candidate_surface,
        )
        draft = {
            "alias_candidate_id": alias_candidate_id,
            "candidate_type": candidate_type,
            "source_method": source_method,
            "rule_id": rule_id,
            "rule_release": rule_release,
            "canonical_surface": canonical_surface,
            "candidate_surface": candidate_surface,
            "document_id": document_id,
            "chunk_id": chunk_id,
            "sentence_id": sentence_id,
            "canonical_start": canonical_start,
            "canonical_end": canonical_end,
            "candidate_start": candidate_start,
            "candidate_end": candidate_end,
            "evidence_text": evidence_text,
            "entity_type": entity_type,
            "candidate_entity_type": candidate_entity_type,
            "scope": scope,
            "confidence": confidence,
            "extractor_release": extractor_release,
            "model_release": model_release,
        }
        contract_hash = alias_contract_hash(draft)
        return cls(**draft, contract_hash=contract_hash)


class AliasDecisionV1(StrictModel):
    alias_candidate_id: str = Field(min_length=1)
    decision: AliasDecisionKind
    decision_reason: str = Field(min_length=1)
    identity_merge_allowed: bool
    retrieval_expansion_allowed: bool
    scope: AliasScope
    ambiguity_status: AmbiguityStatus
    gate_release: str = Field(min_length=1)
    decision_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_decision_consistency(self) -> "AliasDecisionV1":
        if self.decision in {"ACCEPT_IDENTITY", "ACCEPT_TEMPORAL_IDENTITY"}:
            if not self.identity_merge_allowed:
                raise ValueError(
                    f"{self.decision} requires identity_merge_allowed=true"
                )
            if not self.retrieval_expansion_allowed:
                raise ValueError(
                    f"{self.decision} requires retrieval_expansion_allowed=true"
                )
        if self.decision == "REJECT" and self.identity_merge_allowed:
            raise ValueError("REJECT forbids identity_merge_allowed")
        if self.decision == "ACCEPT_RETRIEVAL_ONLY" and self.identity_merge_allowed:
            raise ValueError(
                "ACCEPT_RETRIEVAL_ONLY forbids identity_merge_allowed"
            )
        expected = alias_contract_hash(self.model_dump(exclude={"decision_hash"}))
        if self.decision_hash != expected:
            raise ValueError("decision_hash must match deterministic payload hash")
        return self

    @classmethod
    def create(
        cls,
        *,
        alias_candidate_id: str,
        decision: AliasDecisionKind,
        decision_reason: str,
        identity_merge_allowed: bool,
        retrieval_expansion_allowed: bool,
        scope: AliasScope,
        ambiguity_status: AmbiguityStatus,
        gate_release: str = ALIAS_GATE_RELEASE,
    ) -> "AliasDecisionV1":
        draft = {
            "alias_candidate_id": alias_candidate_id,
            "decision": decision,
            "decision_reason": decision_reason,
            "identity_merge_allowed": identity_merge_allowed,
            "retrieval_expansion_allowed": retrieval_expansion_allowed,
            "scope": scope,
            "ambiguity_status": ambiguity_status,
            "gate_release": gate_release,
        }
        return cls(**draft, decision_hash=alias_contract_hash(draft))


class ParentAliasBundleV1(StrictModel):
    """Parent-scoped aggregation of child alias evidence (Phase 5).

    Never invents identity from co-occurrence or parent summaries.
    """

    parent_alias_bundle_id: str = Field(min_length=1)
    parent_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    canonical_surface: str = Field(min_length=1)
    candidate_surface: str = Field(min_length=1)
    candidate_type: AliasCandidateType
    decision: AliasDecisionKind
    scope: AliasScope
    defining_child_ids: list[str]
    supporting_child_ids: list[str]
    evidence_candidate_ids: list[str]
    ambiguity_status: AmbiguityStatus
    identity_merge_allowed: bool
    bundle_release: str = Field(min_length=1)
    bundle_hash: str = Field(min_length=1)
    entity_type: str | None = None
    reconstruction_method: str | None = None

    @model_validator(mode="after")
    def validate_bundle(self) -> "ParentAliasBundleV1":
        if self.identity_merge_allowed and self.decision not in {
            "ACCEPT_IDENTITY",
            "ACCEPT_TEMPORAL_IDENTITY",
        }:
            raise ValueError(
                "identity_merge_allowed requires ACCEPT_IDENTITY or "
                "ACCEPT_TEMPORAL_IDENTITY"
            )
        if self.decision in {"ACCEPT_IDENTITY", "ACCEPT_TEMPORAL_IDENTITY"}:
            if not self.defining_child_ids and self.reconstruction_method is None:
                raise ValueError(
                    "identity bundle requires defining_child_ids or "
                    "boundary reconstruction_method"
                )
            if not self.evidence_candidate_ids and self.reconstruction_method is None:
                raise ValueError(
                    "identity bundle requires evidence_candidate_ids or "
                    "boundary reconstruction_method"
                )
        expected = alias_contract_hash(self.model_dump(exclude={"bundle_hash"}))
        if self.bundle_hash != expected:
            raise ValueError("bundle_hash must match deterministic payload hash")
        return self

    @classmethod
    def create(
        cls,
        *,
        parent_id: str,
        document_id: str,
        canonical_surface: str,
        candidate_surface: str,
        candidate_type: AliasCandidateType,
        decision: AliasDecisionKind,
        scope: AliasScope,
        defining_child_ids: list[str],
        supporting_child_ids: list[str],
        evidence_candidate_ids: list[str],
        ambiguity_status: AmbiguityStatus,
        identity_merge_allowed: bool,
        bundle_release: str = PARENT_ALIAS_BUNDLE_RELEASE,
        entity_type: str | None = None,
        reconstruction_method: str | None = None,
    ) -> "ParentAliasBundleV1":
        defining = sorted(set(defining_child_ids))
        supporting = sorted(set(supporting_child_ids))
        evidence = sorted(set(evidence_candidate_ids))
        parent_alias_bundle_id = (
            "parent-alias:"
            + alias_contract_hash(
                {
                    "parent_id": parent_id,
                    "document_id": document_id,
                    "canonical_surface": " ".join(canonical_surface.lower().split()),
                    "candidate_surface": " ".join(candidate_surface.lower().split()),
                    "candidate_type": candidate_type,
                    "decision": decision,
                    "defining_child_ids": defining,
                    "evidence_candidate_ids": evidence,
                    "reconstruction_method": reconstruction_method or "",
                }
            )
        )
        draft = {
            "parent_alias_bundle_id": parent_alias_bundle_id,
            "parent_id": parent_id,
            "document_id": document_id,
            "canonical_surface": canonical_surface,
            "candidate_surface": candidate_surface,
            "candidate_type": candidate_type,
            "decision": decision,
            "scope": scope,
            "defining_child_ids": defining,
            "supporting_child_ids": supporting,
            "evidence_candidate_ids": evidence,
            "ambiguity_status": ambiguity_status,
            "identity_merge_allowed": identity_merge_allowed,
            "bundle_release": bundle_release,
            "entity_type": entity_type,
            "reconstruction_method": reconstruction_method,
        }
        return cls(**draft, bundle_hash=alias_contract_hash(draft))


class DocumentEntityV1(StrictModel):
    document_entity_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    mention_ids: list[str]
    accepted_alias_ids: list[str]
    retrieval_variant_ids: list[str]
    description_records: list[str]
    defining_child_ids: list[str]
    supporting_child_ids: list[str]
    entity_type: str | None = None
    cluster_release: str = Field(min_length=1)
    cluster_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_cluster_hash(self) -> "DocumentEntityV1":
        expected = alias_contract_hash(self.model_dump(exclude={"cluster_hash"}))
        if self.cluster_hash != expected:
            raise ValueError("cluster_hash must match deterministic payload hash")
        return self

    @classmethod
    def create(
        cls,
        *,
        document_id: str,
        canonical_name: str,
        mention_ids: list[str],
        accepted_alias_ids: list[str],
        retrieval_variant_ids: list[str],
        description_records: list[str],
        defining_child_ids: list[str] | None = None,
        supporting_child_ids: list[str] | None = None,
        entity_type: str | None = None,
        cluster_release: str = ALIAS_CLUSTER_RELEASE,
    ) -> "DocumentEntityV1":
        mentions = sorted(set(mention_ids))
        accepted = sorted(set(accepted_alias_ids))
        variants = sorted(set(retrieval_variant_ids))
        descriptions = sorted(set(description_records))
        defining = sorted(set(defining_child_ids or []))
        supporting = sorted(set(supporting_child_ids or []))
        document_entity_id = (
            "docent:"
            + alias_contract_hash(
                {
                    "document_id": document_id,
                    "canonical_name": " ".join(canonical_name.lower().split()),
                    "mention_ids": mentions,
                    "accepted_alias_ids": accepted,
                    "defining_child_ids": defining,
                }
            )
        )
        draft = {
            "document_entity_id": document_entity_id,
            "document_id": document_id,
            "canonical_name": canonical_name,
            "mention_ids": mentions,
            "accepted_alias_ids": accepted,
            "retrieval_variant_ids": variants,
            "description_records": descriptions,
            "defining_child_ids": defining,
            "supporting_child_ids": supporting,
            "entity_type": entity_type,
            "cluster_release": cluster_release,
        }
        return cls(**draft, cluster_hash=alias_contract_hash(draft))


CorpusMergeDecisionKind = Literal[
    "MERGE",
    "LINK_TEMPORAL_IDENTITY",
    "REVIEW",
    "REJECT",
]

CorpusMergeAcronymStatus = Literal[
    "not_applicable",
    "validated_long_form_match",
    "bare_acronym_only",
    "conflicting_expansion",
    "document_scoped",
]

CorpusMergeTemporalStatus = Literal[
    "not_applicable",
    "former_name_link",
    "current_name_match",
    "temporal_conflict",
]


class CorpusMergeDecisionV1(StrictModel):
    """Auditable pair-level corpus merge decision (Phase 6).

    Every union in corpus clustering must cite one accepted merge decision.
    """

    merge_decision_id: str = Field(min_length=1)
    left_document_entity_id: str = Field(min_length=1)
    right_document_entity_id: str = Field(min_length=1)
    candidate_method: str = Field(min_length=1)
    supporting_alias_decision_ids: list[str]
    supporting_document_ids: list[str]
    supporting_evidence_ids: list[str]
    entity_type_compatible: bool
    canonical_name_compatible: bool
    acronym_status: CorpusMergeAcronymStatus
    temporal_status: CorpusMergeTemporalStatus
    ambiguity_status: AmbiguityStatus
    decision: CorpusMergeDecisionKind
    decision_reason: str = Field(min_length=1)
    identity_merge_allowed: bool
    merge_release: str = Field(min_length=1)
    decision_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_merge_decision(self) -> "CorpusMergeDecisionV1":
        if self.decision in {"MERGE", "LINK_TEMPORAL_IDENTITY"}:
            if not self.identity_merge_allowed:
                raise ValueError(
                    f"{self.decision} requires identity_merge_allowed=true"
                )
            if not self.supporting_document_ids:
                raise ValueError(f"{self.decision} requires supporting_document_ids")
        if self.decision in {"REJECT", "REVIEW"} and self.identity_merge_allowed:
            raise ValueError(f"{self.decision} forbids identity_merge_allowed")
        expected = alias_contract_hash(self.model_dump(exclude={"decision_hash"}))
        if self.decision_hash != expected:
            raise ValueError("decision_hash must match deterministic payload hash")
        return self

    @classmethod
    def create(
        cls,
        *,
        left_document_entity_id: str,
        right_document_entity_id: str,
        candidate_method: str,
        supporting_alias_decision_ids: list[str],
        supporting_document_ids: list[str],
        supporting_evidence_ids: list[str],
        entity_type_compatible: bool,
        canonical_name_compatible: bool,
        acronym_status: CorpusMergeAcronymStatus,
        temporal_status: CorpusMergeTemporalStatus,
        ambiguity_status: AmbiguityStatus,
        decision: CorpusMergeDecisionKind,
        decision_reason: str,
        identity_merge_allowed: bool,
        merge_release: str = CORPUS_MERGE_RELEASE,
    ) -> "CorpusMergeDecisionV1":
        left, right = sorted(
            [left_document_entity_id, right_document_entity_id]
        )
        alias_ids = sorted(set(supporting_alias_decision_ids))
        doc_ids = sorted(set(supporting_document_ids))
        evidence_ids = sorted(set(supporting_evidence_ids))
        merge_decision_id = (
            "corpusmerge:"
            + alias_contract_hash(
                {
                    "left_document_entity_id": left,
                    "right_document_entity_id": right,
                    "candidate_method": candidate_method,
                    "decision": decision,
                    "decision_reason": decision_reason,
                    "supporting_alias_decision_ids": alias_ids,
                    "supporting_evidence_ids": evidence_ids,
                }
            )
        )
        draft = {
            "merge_decision_id": merge_decision_id,
            "left_document_entity_id": left,
            "right_document_entity_id": right,
            "candidate_method": candidate_method,
            "supporting_alias_decision_ids": alias_ids,
            "supporting_document_ids": doc_ids,
            "supporting_evidence_ids": evidence_ids,
            "entity_type_compatible": entity_type_compatible,
            "canonical_name_compatible": canonical_name_compatible,
            "acronym_status": acronym_status,
            "temporal_status": temporal_status,
            "ambiguity_status": ambiguity_status,
            "decision": decision,
            "decision_reason": decision_reason,
            "identity_merge_allowed": identity_merge_allowed,
            "merge_release": merge_release,
        }
        return cls(**draft, decision_hash=alias_contract_hash(draft))


class TemporalAliasRecordV1(StrictModel):
    """Former/current name chronology retained on corpus entities."""

    former_name: str = Field(min_length=1)
    current_name: str = Field(min_length=1)
    valid_before: str | None = None
    valid_after: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class CorpusEntityV1(StrictModel):
    corpus_entity_id: str = Field(min_length=1)
    corpus_id: str = Field(min_length=1)
    canonical_name: str = Field(min_length=1)
    entity_type: str | None = None
    document_entity_ids: list[str]
    accepted_merge_decision_ids: list[str]
    trusted_aliases: list[str]
    temporal_aliases: list[str]
    retrieval_surface_variants: list[str]
    descriptions: list[str]
    related_terms: list[str]
    ambiguous_aliases: list[str]
    conflicting_candidates: list[str]
    source_document_ids: list[str]
    supporting_alias_decision_ids: list[str]
    canonical_name_rule: str = Field(min_length=1)
    cluster_release: str = Field(min_length=1)
    cluster_hash: str = Field(min_length=1)
    # Structured temporal links (serialized deterministically for hashing).
    temporal_identity_records: list[dict] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_cluster_hash(self) -> "CorpusEntityV1":
        expected = alias_contract_hash(self.model_dump(exclude={"cluster_hash"}))
        if self.cluster_hash != expected:
            raise ValueError("cluster_hash must match deterministic payload hash")
        return self

    @classmethod
    def create(
        cls,
        *,
        corpus_id: str,
        canonical_name: str,
        document_entity_ids: list[str],
        accepted_merge_decision_ids: list[str] | None = None,
        trusted_aliases: list[str] | None = None,
        temporal_aliases: list[str] | None = None,
        retrieval_surface_variants: list[str] | None = None,
        descriptions: list[str] | None = None,
        related_terms: list[str] | None = None,
        ambiguous_aliases: list[str] | None = None,
        conflicting_candidates: list[str] | None = None,
        source_document_ids: list[str] | None = None,
        supporting_alias_decision_ids: list[str] | None = None,
        canonical_name_rule: str = "lexicographic_tiebreak",
        entity_type: str | None = None,
        temporal_identity_records: list[dict] | None = None,
        cluster_release: str = CORPUS_CLUSTER_RELEASE,
    ) -> "CorpusEntityV1":
        doc_ents = sorted(set(document_entity_ids))
        merges = sorted(set(accepted_merge_decision_ids or []))
        trusted = sorted({a for a in (trusted_aliases or []) if a})
        temporal = sorted({a for a in (temporal_aliases or []) if a})
        variants = sorted({a for a in (retrieval_surface_variants or []) if a})
        related = sorted({a for a in (related_terms or []) if a})
        descs = sorted({a for a in (descriptions or []) if a})
        ambiguous = sorted({a for a in (ambiguous_aliases or []) if a})
        conflicts = sorted({a for a in (conflicting_candidates or []) if a})
        source_docs = sorted(set(source_document_ids or []))
        support = sorted(set(supporting_alias_decision_ids or []))
        temporal_recs = sorted(
            temporal_identity_records or [],
            key=lambda r: (
                str(r.get("former_name", "")).lower(),
                str(r.get("current_name", "")).lower(),
                str(r.get("valid_after", "") or ""),
            ),
        )
        corpus_entity_id = (
            "corpent:"
            + alias_contract_hash(
                {
                    "corpus_id": corpus_id,
                    "canonical_name": " ".join(canonical_name.lower().split()),
                    "document_entity_ids": doc_ents,
                    "trusted_aliases": trusted,
                    "accepted_merge_decision_ids": merges,
                    "canonical_name_rule": canonical_name_rule,
                }
            )
        )
        draft = {
            "corpus_entity_id": corpus_entity_id,
            "corpus_id": corpus_id,
            "canonical_name": canonical_name,
            "entity_type": entity_type,
            "document_entity_ids": doc_ents,
            "accepted_merge_decision_ids": merges,
            "trusted_aliases": trusted,
            "temporal_aliases": temporal,
            "retrieval_surface_variants": variants,
            "descriptions": descs,
            "related_terms": related,
            "ambiguous_aliases": ambiguous,
            "conflicting_candidates": conflicts,
            "source_document_ids": source_docs,
            "supporting_alias_decision_ids": support,
            "canonical_name_rule": canonical_name_rule,
            "cluster_release": cluster_release,
            "temporal_identity_records": temporal_recs,
        }
        return cls(**draft, cluster_hash=alias_contract_hash(draft))

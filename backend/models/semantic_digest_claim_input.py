"""Noncanonical child-compilation inputs for atomic semantic-digest packets."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from models.artifact_envelope import ArtifactEnvelope
from models.claim_record import ClaimCompilationV1
from models.hash_taxonomy import canonical_json_v1, namespace_hash
from models.semantic_artifacts import EvidenceRef

EXPORT_SCHEMA_VERSION = "semantic_digest_claim_compilation_export.v1"
ROW_SCHEMA_VERSION = "semantic_digest_claim_compilation_row.v1"
COMPILATION_COLLECTION = "semantic_digest_claim_compilations"
COMPILATION_ARTIFACT_TYPE = "semantic_digest_claim_compilation_input"


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        protected_namespaces=(),
        populate_by_name=True,
    )


def evidence_set_hash(evidence_refs: list[EvidenceRef]) -> str:
    return namespace_hash(
        "evidence-set",
        [
            item.model_dump(mode="python")
            for item in sorted(evidence_refs, key=lambda row: row.evidence_ref_id)
        ],
    )


def _validate_candidate_payload(
    *,
    document_id: str,
    source_version_id: str,
    child_id: str,
    source_text_hash: str,
    observation_recipe_hash: str,
    compiler_recipe_hash: str,
    evidence_refs: list[EvidenceRef],
    compilation: ClaimCompilationV1,
) -> None:
    if compilation.document_id != document_id or compilation.child_id != child_id:
        raise ValueError("compilation ownership does not match candidate ownership")
    if compilation.compiler_recipe_hash != compiler_recipe_hash:
        raise ValueError("compiler recipe metadata does not match compilation")
    if not source_text_hash.startswith("sha256:"):
        raise ValueError("source text hash must be canonical")
    if not observation_recipe_hash.startswith("sha256:"):
        raise ValueError("observation recipe hash must be canonical")
    ids = [item.evidence_ref_id for item in evidence_refs]
    if len(ids) != len(set(ids)):
        raise ValueError("evidence references must be unique")
    evidence = {item.evidence_ref_id: item for item in evidence_refs}
    for item in evidence_refs:
        if (
            item.source_version_id != source_version_id
            or item.hierarchy_node_id != child_id
        ):
            raise ValueError("evidence ownership does not match child candidate")
    for claim in compilation.claims:
        if len(claim.evidence_sentence_ids) != 1:
            raise ValueError(
                "atomic packet v1 requires one evidence sentence per claim"
            )
        evidence_id = claim.evidence_sentence_ids[0]
        if evidence_id not in evidence:
            raise ValueError("claim references evidence absent from candidate")
        if claim.proposition_text != evidence[evidence_id].quote:
            raise ValueError("claim proposition is not its exact evidence quote")
    for link in compilation.links:
        if set(link.evidence_sentence_ids) - set(evidence):
            raise ValueError("claim link references evidence absent from candidate")


class CompiledChildCandidateExportV1(StrictModel):
    schema_version: Literal["semantic_digest_claim_compilation_export.v1"]
    corpus_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_version_id: str = Field(min_length=1)
    child_id: str = Field(min_length=1)
    source_text_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observation_bundle_id: str = Field(min_length=1)
    observation_recipe_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    local_extraction_recipe_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    normalization_registry_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    compiler_version: Literal["claim_compiler.v2"]
    compiler_recipe_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    spacy_library_version: Literal["3.8.14"]
    spacy_model: Literal["en_core_web_sm"]
    spacy_model_version: Literal["3.8.0"]
    parser_version: Literal["spacy:3.8.14;model:3.8.0"]
    evidence_refs: list[EvidenceRef]
    compilation: ClaimCompilationV1

    @model_validator(mode="after")
    def validate_candidate(self) -> "CompiledChildCandidateExportV1":
        _validate_candidate_payload(
            document_id=self.document_id,
            source_version_id=self.source_version_id,
            child_id=self.child_id,
            source_text_hash=self.source_text_hash,
            observation_recipe_hash=self.observation_recipe_hash,
            compiler_recipe_hash=self.compiler_recipe_hash,
            evidence_refs=self.evidence_refs,
            compilation=self.compilation,
        )
        return self


class ClaimCompilationMaterializationRowV1(StrictModel):
    row_id: str = Field(alias="_id", min_length=1)
    schema_version: Literal["semantic_digest_claim_compilation_row.v1"]
    corpus_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    source_version_id: str = Field(min_length=1)
    child_id: str = Field(min_length=1)
    source_text_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    observation_bundle_id: str = Field(min_length=1)
    observation_recipe_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    local_extraction_recipe_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    normalization_registry_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    compiler_version: Literal["claim_compiler.v2"]
    compiler_recipe_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    spacy_library_version: Literal["3.8.14"]
    spacy_model: Literal["en_core_web_sm"]
    spacy_model_version: Literal["3.8.0"]
    parser_version: Literal["spacy:3.8.14;model:3.8.0"]
    canonical_write: Literal[False]
    status: Literal["candidate"]
    evidence_refs: list[EvidenceRef]
    envelope: ArtifactEnvelope[ClaimCompilationV1]

    @model_validator(mode="after")
    def validate_materialized_row(self) -> "ClaimCompilationMaterializationRowV1":
        _validate_candidate_payload(
            document_id=self.document_id,
            source_version_id=self.source_version_id,
            child_id=self.child_id,
            source_text_hash=self.source_text_hash,
            observation_recipe_hash=self.observation_recipe_hash,
            compiler_recipe_hash=self.compiler_recipe_hash,
            evidence_refs=self.evidence_refs,
            compilation=self.envelope.body,
        )
        if self.row_id != self.envelope.artifact_revision_id:
            raise ValueError("row ID must equal envelope artifact revision ID")
        if self.envelope.artifact_type != COMPILATION_ARTIFACT_TYPE:
            raise ValueError("materialized row has the wrong artifact type")
        ownership = self.envelope.ownership
        if (
            ownership.corpus_id != self.corpus_id
            or ownership.doc_id != self.document_id
            or ownership.source_version_id != self.source_version_id
            or ownership.hierarchy_node_id != self.child_id
        ):
            raise ValueError("envelope ownership does not match row ownership")
        if self.envelope.integrity.recipe_hash != self.compiler_recipe_hash:
            raise ValueError("envelope recipe hash does not match compiler recipe")
        if self.envelope.integrity.evidence_set_hash != evidence_set_hash(
            self.evidence_refs
        ):
            raise ValueError("envelope evidence-set hash does not match evidence")
        if self.envelope.artifact_state != "candidate":
            raise ValueError("claim compilation input must remain candidate")
        if self.envelope.knowledge_status is not None:
            raise ValueError("claim compilation input cannot assert knowledge status")
        return self


def parse_materialized_row_document(
    value: dict[str, object],
) -> ClaimCompilationMaterializationRowV1:
    """Strictly revalidate a BSON-loaded row using JSON transport semantics.

    MongoDB returns arrays for the envelope's immutable tuple fields. Pydantic's
    strict Python-mode validator correctly rejects list-to-tuple coercion, while
    strict JSON-mode validation accepts JSON arrays as tuples. Canonical JSON
    also makes timestamp conversion explicit instead of weakening validation.
    """

    return ClaimCompilationMaterializationRowV1.model_validate_json(
        canonical_json_v1(value)
    )

"""Strict shared envelope for every new durable semantic artifact.

Implements FINAL_SCHEMA_METADATA_ARCHITECTURE_2026-07-13.md
``polymath.artifact_envelope.v1`` literally. The body must be a typed Pydantic
model; its generated JSON Schema and canonical body are hash-checked at model
construction. Validation, lifecycle, and provider telemetry remain outside
body identity, so revalidation cannot silently mint a different semantic
revision and a changed body cannot reuse the old revision identifier.
"""

from __future__ import annotations

import re
from typing import Generic, Literal, TypeVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from models.hash_taxonomy import namespace_hash
from models.identifier_recipes import artifact_revision_id

ENVELOPE_VERSION = "polymath.artifact_envelope.v1"

ArtifactState = Literal[
    "candidate",
    "validated",
    "active",
    "rejected",
    "quarantined",
    "superseded",
]
KnowledgeStatus = Literal[
    "asserted",
    "entailed",
    "cross_passage_synthesis",
    "structural_analogy",
    "hypothetical",
]
ProducerKind = Literal[
    "python_rule",
    "spacy",
    "zero_shot",
    "provider_llm",
    "human",
    "migration",
]

_KNOWLEDGE_BEARING_ARTIFACT_TYPES = frozenset(
    {"claim_assertion", "semantic_digest", "analogy_card"}
)
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        protected_namespaces=(),
    )


class ArtifactOwnership(StrictFrozenModel):
    corpus_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    source_version_id: str = Field(min_length=1)
    hierarchy_node_id: str | None = None


class ArtifactIntegrity(StrictFrozenModel):
    body_hash: str
    evidence_set_hash: str | None
    input_set_hash: str
    recipe_hash: str
    registry_set_hash: str | None


class ArtifactProvenance(StrictFrozenModel):
    work_id: str = Field(min_length=1)
    attempt_id: str | None
    raw_artifact_ids: tuple[str, ...] = ()
    producer_kind: ProducerKind
    engine: str = Field(min_length=1)
    model_id: str | None
    model_revision: str | None
    prompt_id: str | None
    prompt_hash: str | None
    compiler_version: str = Field(min_length=1)
    parser_version: str | None
    rule_pack_version: str | None
    run_id: str = Field(min_length=1)


class ArtifactValidation(StrictFrozenModel):
    contract_valid: bool
    evidence_valid: bool
    registry_valid: bool
    policy_valid: bool
    validator_version: str = Field(min_length=1)
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class ArtifactLifecycle(StrictFrozenModel):
    created_at: AwareDatetime
    validated_at: AwareDatetime | None
    activated_at: AwareDatetime | None
    supersedes_revision_id: str | None
    superseded_at: AwareDatetime | None


BodyT = TypeVar("BodyT", bound=BaseModel)


def schema_hash_for_body(body: BaseModel) -> str:
    if not isinstance(body, BaseModel):
        raise TypeError("artifact body must be a typed Pydantic model")
    return namespace_hash("schema", body.__class__.model_json_schema())


def body_hash_for_body(body: BaseModel) -> str:
    if not isinstance(body, BaseModel):
        raise TypeError("artifact body must be a typed Pydantic model")
    return namespace_hash("body", body.model_dump(mode="python"))


def _validate_hash(value: str | None, field_name: str) -> None:
    if value is not None and not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be canonical sha256:<64 lowercase hex>")


class ArtifactEnvelope(StrictFrozenModel, Generic[BodyT]):
    envelope_version: Literal["polymath.artifact_envelope.v1"] = ENVELOPE_VERSION
    artifact_type: str = Field(min_length=1)
    schema_id: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    schema_hash: str
    artifact_id: str = Field(min_length=1)
    artifact_revision_id: str = Field(min_length=1)
    artifact_state: ArtifactState
    knowledge_status: KnowledgeStatus | None
    ownership: ArtifactOwnership
    integrity: ArtifactIntegrity
    provenance: ArtifactProvenance
    validation: ArtifactValidation
    lifecycle: ArtifactLifecycle
    body: BodyT

    @model_validator(mode="after")
    def validate_envelope_identity(self) -> "ArtifactEnvelope[BodyT]":
        if not isinstance(self.body, BaseModel):
            raise TypeError(
                "artifact body must be a typed Pydantic model; bare dicts are forbidden"
            )

        hash_fields = {
            "schema_hash": self.schema_hash,
            "body_hash": self.integrity.body_hash,
            "evidence_set_hash": self.integrity.evidence_set_hash,
            "input_set_hash": self.integrity.input_set_hash,
            "recipe_hash": self.integrity.recipe_hash,
            "registry_set_hash": self.integrity.registry_set_hash,
            "prompt_hash": self.provenance.prompt_hash,
        }
        for name, value in hash_fields.items():
            _validate_hash(value, name)

        expected_schema_hash = schema_hash_for_body(self.body)
        if self.schema_hash != expected_schema_hash:
            raise ValueError("schema_hash does not match body model_json_schema()")

        expected_body_hash = body_hash_for_body(self.body)
        if self.integrity.body_hash != expected_body_hash:
            raise ValueError("integrity.body_hash does not match the typed body")

        expected_revision = artifact_revision_id(
            self.artifact_id,
            self.schema_hash,
            self.integrity.body_hash,
        )
        if self.artifact_revision_id != expected_revision:
            raise ValueError(
                "artifact_revision_id does not match artifact_id + schema_hash + body_hash"
            )

        if (
            self.artifact_type in _KNOWLEDGE_BEARING_ARTIFACT_TYPES
            and self.knowledge_status is None
        ):
            raise ValueError(
                f"{self.artifact_type} requires an explicit knowledge_status"
            )
        return self


def make_artifact_envelope(
    *,
    artifact_type: str,
    schema_id: str,
    schema_version: str,
    artifact_id: str,
    artifact_state: ArtifactState,
    knowledge_status: KnowledgeStatus | None,
    ownership: ArtifactOwnership,
    input_set_hash: str,
    recipe_hash: str,
    evidence_set_hash: str | None,
    registry_set_hash: str | None,
    provenance: ArtifactProvenance,
    validation: ArtifactValidation,
    lifecycle: ArtifactLifecycle,
    body: BodyT,
) -> ArtifactEnvelope[BodyT]:
    """Build a self-consistent envelope from one typed immutable body."""

    if not isinstance(body, BaseModel):
        raise TypeError("artifact body must be a typed Pydantic model")
    schema_hash = schema_hash_for_body(body)
    body_hash = body_hash_for_body(body)
    revision_id = artifact_revision_id(artifact_id, schema_hash, body_hash)
    envelope_type = ArtifactEnvelope[body.__class__]
    return envelope_type(
        artifact_type=artifact_type,
        schema_id=schema_id,
        schema_version=schema_version,
        schema_hash=schema_hash,
        artifact_id=artifact_id,
        artifact_revision_id=revision_id,
        artifact_state=artifact_state,
        knowledge_status=knowledge_status,
        ownership=ownership,
        integrity=ArtifactIntegrity(
            body_hash=body_hash,
            evidence_set_hash=evidence_set_hash,
            input_set_hash=input_set_hash,
            recipe_hash=recipe_hash,
            registry_set_hash=registry_set_hash,
        ),
        provenance=provenance,
        validation=validation,
        lifecycle=lifecycle,
        body=body,
    )

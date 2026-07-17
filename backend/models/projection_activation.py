"""Versioned contracts for crash-recoverable semantic projection activation.

The frozen v1 manifest/outbox models intentionally describe identity and a
minimal state machine only.  They do not identify the physical projection
target or the durable source row, and they cannot recover an abandoned lease.
Activation therefore uses additive v2 contracts rather than silently changing
the meaning of persisted v1 documents.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from models.hash_taxonomy import namespace_hash
from models.projection_manifest import QDRANT_FAMILIES, SearchCompat

MANIFEST_ACTIVATION_VERSION = "projection_manifest.v2"
OUTBOX_ACTIVATION_VERSION = "projection_outbox.v2"

ActivationOp = Literal["upsert", "delete"]
ActivationState = Literal["pending", "in_flight", "applied", "failed", "dead"]


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        protected_namespaces=(),
    )


class ProjectionTargetV2(StrictFrozenModel):
    """Exact physical target needed to replay a projection without code lore."""

    collection_name: str = Field(min_length=1)
    vector_name: str = Field(min_length=1)


class ActivationEmbeddingProfileV2(StrictFrozenModel):
    model_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    dims: int = Field(gt=0)
    quantization: Literal["float32", "float16", "mxfp8", "binary"]
    instruction_version: str = Field(min_length=1)
    document_side_instruction: Literal["raw"] = "raw"
    sparse_recipe_version: Literal["none"] = "none"


class ProjectionManifestV2(StrictFrozenModel):
    schema_version: Literal["projection_manifest.v2"] = MANIFEST_ACTIVATION_VERSION
    manifest_id: str = Field(pattern=r"^projm:[0-9a-f]{64}$")
    store: Literal["qdrant"] = "qdrant"
    family: str = Field(min_length=1)
    representation_role: str = Field(min_length=1)
    source_schema_hashes: dict[str, str]
    payload_schema_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    embedding_profile: ActivationEmbeddingProfileV2
    search_compat: SearchCompat
    target: ProjectionTargetV2
    recipe_version: str = Field(min_length=1)
    rollback_predecessor: str | None = Field(
        default=None, pattern=r"^projm:[0-9a-f]{64}$"
    )

    def identity_body(self) -> dict:
        return self.model_dump(exclude={"manifest_id", "rollback_predecessor"})

    @property
    def projection_profile_hash(self) -> str:
        return namespace_hash("projection-profile", self.identity_body())

    @model_validator(mode="after")
    def validate_identity(self) -> "ProjectionManifestV2":
        if self.family not in QDRANT_FAMILIES:
            raise ValueError(f"unknown qdrant projection family {self.family!r}")
        if not self.source_schema_hashes or any(
            not key
            or not isinstance(value, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
            for key, value in self.source_schema_hashes.items()
        ):
            raise ValueError("source_schema_hashes must contain canonical hashes")
        expected = "projm:" + self.projection_profile_hash.split(":", 1)[1]
        if self.manifest_id != expected:
            raise ValueError("manifest_id does not match the v2 projection profile")
        return self


class ProjectionSourceLocatorV2(StrictFrozenModel):
    """Exact, provenance-closed source for one semantic digest projection."""

    source_kind: Literal["semantic_digest_cache"]
    source_collection: Literal["semantic_digest_cache"]
    source_id: str = Field(min_length=1)
    ownership_collection: Literal["semantic_digest_jobs"]
    ownership_id: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    corpus_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    source_version_id: str = Field(min_length=1)
    parent_id: str = Field(min_length=1)
    parent_text_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_child_ids_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_child_count: int = Field(ge=1)


class ProjectionApplicationReceiptV2(StrictFrozenModel):
    schema_version: Literal[
        "projection_application_receipt.v1"
    ] = "projection_application_receipt.v1"
    target_collection: str = Field(min_length=1)
    vector_name: str = Field(min_length=1)
    point_id: str = Field(min_length=1)
    projected_payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    operation_id: str | None = None
    applied_at: AwareDatetime
    reconciled: Literal[True]


class ProjectionOutboxV2(StrictFrozenModel):
    schema_version: Literal["projection_outbox.v2"] = OUTBOX_ACTIVATION_VERSION
    outbox_id: str = Field(pattern=r"^outbox:[0-9a-f]{64}$")
    artifact_revision_id: str = Field(pattern=r"^rev:[0-9a-f]{64}$")
    manifest_id: str = Field(pattern=r"^projm:[0-9a-f]{64}$")
    point_id: str = Field(min_length=1)
    projected_payload_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    op: ActivationOp
    source: ProjectionSourceLocatorV2
    state: ActivationState = "pending"
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=5, ge=1)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    lease_owner: str | None = None
    lease_expires_at: AwareDatetime | None = None
    applied_at: AwareDatetime | None = None
    application_receipt: ProjectionApplicationReceiptV2 | None = None
    last_error: str | None = None

    @model_validator(mode="after")
    def validate_state_fields(self) -> "ProjectionOutboxV2":
        if self.state == "in_flight":
            if not self.lease_owner or self.lease_expires_at is None:
                raise ValueError("in_flight entries require an active lease")
        elif self.lease_owner is not None or self.lease_expires_at is not None:
            raise ValueError("only in_flight entries may carry a lease")
        if self.state == "applied" and (
            self.applied_at is None or self.application_receipt is None
        ):
            raise ValueError("applied entries require applied_at and a receipt")
        if self.state != "applied" and (
            self.applied_at is not None or self.application_receipt is not None
        ):
            raise ValueError("only applied entries may carry apply receipts")
        if self.state in {"failed", "dead"} and not self.last_error:
            raise ValueError("failed/dead entries require last_error")
        if self.state not in {"failed", "dead"} and self.last_error is not None:
            raise ValueError("last_error is restricted to failed/dead entries")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


def make_activation_manifest(**kwargs) -> ProjectionManifestV2:
    body = {
        "schema_version": MANIFEST_ACTIVATION_VERSION,
        "store": "qdrant",
        **kwargs,
    }
    identity_body = {
        key: value.model_dump(mode="python") if isinstance(value, BaseModel) else value
        for key, value in body.items()
        if key not in {"manifest_id", "rollback_predecessor"}
    }
    digest = namespace_hash("projection-profile", identity_body).split(":", 1)[1]
    return ProjectionManifestV2(manifest_id=f"projm:{digest}", **body)


def activation_outbox_id(
    artifact_revision_id: str,
    manifest_id: str,
    op: ActivationOp,
) -> str:
    digest = namespace_hash(
        "work",
        {
            "kind": OUTBOX_ACTIVATION_VERSION,
            "artifact_revision_id": artifact_revision_id,
            "manifest_id": manifest_id,
            "op": op,
        },
    ).split(":", 1)[1]
    return f"outbox:{digest}"


def make_activation_entry(
    *,
    artifact_revision_id: str,
    manifest_id: str,
    point_id: str,
    projected_payload_hash: str,
    source: ProjectionSourceLocatorV2,
    now: datetime,
    op: ActivationOp = "upsert",
    max_attempts: int = 5,
) -> ProjectionOutboxV2:
    return ProjectionOutboxV2(
        outbox_id=activation_outbox_id(artifact_revision_id, manifest_id, op),
        artifact_revision_id=artifact_revision_id,
        manifest_id=manifest_id,
        point_id=point_id,
        projected_payload_hash=projected_payload_hash,
        op=op,
        source=source,
        max_attempts=max_attempts,
        created_at=now,
        updated_at=now,
    )

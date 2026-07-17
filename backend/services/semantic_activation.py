"""Governed activation of purchased semantic digests into Tier-0 routing.

Mongo remains authoritative.  The planner validates cache/job/source closure,
persists an immutable v2 manifest and deterministic outbox intent, and the
worker applies only new, profile-bound Qdrant point IDs.  A crash after Qdrant
success but before the Mongo acknowledgement is safe: the lease expires and
the same point is idempotently upserted again.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any, Iterable, Literal

from pydantic import BaseModel
from pymongo import ASCENDING, ReturnDocument
from qdrant_client import models as qm

from config import get_settings
from models.hash_taxonomy import namespace_hash
from models.identifier_recipes import artifact_revision_id, projection_point_id
from models.projection_activation import (
    ActivationEmbeddingProfileV2,
    ProjectionApplicationReceiptV2,
    ProjectionManifestV2,
    ProjectionOutboxV2,
    ProjectionSourceLocatorV2,
    ProjectionTargetV2,
    make_activation_entry,
    make_activation_manifest,
)
from models.projection_manifest import SearchCompat
from models.semantic_digest import SemanticDigestV1
from services.semantic_gateway import (
    SemanticGatewayProvenance,
    semantic_digest_cache_key,
    semantic_digest_prompt_hash,
    semantic_digest_repair_prompt_hash,
    semantic_digest_schema_hash,
)

MANIFEST_COLLECTION = "projection_manifests"
OUTBOX_COLLECTION = "projection_outbox"
DIGEST_CACHE_COLLECTION = "semantic_digest_cache"
DIGEST_JOB_COLLECTION = "semantic_digest_jobs"
DIGEST_REPRESENTATION_ROLE = "semantic_digest"
DIGEST_RECIPE_VERSION = "semantic_digest_tier0_projection.v1"
TERMINAL_DIGEST_QUARANTINES = {
    ("rejected", "faithfulness_rejected_unsupported_synthesis"),
}

DIGEST_TIER0_PAYLOAD_SCHEMA_V1 = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "semantic_digest_tier0_payload.v1",
    "description": (
        "Document-routing metadata only. A semantic digest point may select a "
        "document but is never answer evidence; retrieval must descend to source chunks."
    ),
    "type": "object",
    "additionalProperties": False,
    "required": [
        "payload_schema_version",
        "corpus_id",
        "doc_id",
        "parent_id",
        "chunk_type",
        "summary",
        "concepts",
        "section_ids",
        "artifact_id",
        "artifact_revision_id",
        "projection_role",
        "projection_manifest_id",
        "projection_profile_hash",
        "source_cache_key",
        "source_job_id",
        "source_version_id",
        "model_id",
        "schema_version",
        "schema_hash",
        "prompt_version",
        "prompt_hash",
        "output_hash",
        "provenance_closure",
        "projected_payload_hash",
    ],
    "properties": {
        "payload_schema_version": {"const": "semantic_digest_tier0_payload.v1"},
        "corpus_id": {"type": "string", "minLength": 1},
        "doc_id": {"type": "string", "minLength": 1},
        "parent_id": {"type": "string", "minLength": 1},
        "chunk_type": {"const": "semantic_digest"},
        "summary": {
            "type": "string",
            "minLength": 3,
            "description": "Raw document-side summary followed by central thesis.",
        },
        "concepts": {
            "type": "array",
            "maxItems": 0,
            "description": "Proposal-only digest concepts are intentionally not projected.",
        },
        "section_ids": {"type": "array", "items": {"type": "string"}},
        "artifact_id": {"type": "string", "minLength": 1},
        "artifact_revision_id": {"type": "string", "pattern": "^rev:[0-9a-f]{64}$"},
        "projection_role": {"const": "semantic_digest"},
        "projection_manifest_id": {"type": "string", "pattern": "^projm:[0-9a-f]{64}$"},
        "projection_profile_hash": {
            "type": "string",
            "pattern": "^sha256:[0-9a-f]{64}$",
        },
        "source_cache_key": {"type": "string", "minLength": 1},
        "source_job_id": {"type": "string", "minLength": 1},
        "source_version_id": {"type": "string", "minLength": 1},
        "model_id": {"type": "string", "minLength": 1},
        "schema_version": {"const": "semantic_digest.v1"},
        "schema_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "prompt_version": {"type": "string", "minLength": 1},
        "prompt_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "output_hash": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
        "provenance_closure": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "mode",
                "job_id",
                "cache_key",
                "adopted_prompt_version",
                "adopted_repair_prompt_version",
            ],
            "properties": {
                "mode": {
                    "enum": [
                        "job_prompt_version_labels_exact",
                        "legacy_missing_job_prompt_version_labels",
                    ]
                },
                "job_id": {"type": "string", "minLength": 1},
                "cache_key": {"type": "string", "minLength": 1},
                "adopted_prompt_version": {"type": "string", "minLength": 1},
                "adopted_repair_prompt_version": {
                    "type": "string",
                    "minLength": 1,
                },
            },
        },
        "projected_payload_hash": {
            "type": "string",
            "pattern": "^sha256:[0-9a-f]{64}$",
            "description": "Hash of this payload with projected_payload_hash omitted.",
        },
    },
}
DIGEST_TIER0_PAYLOAD_SCHEMA_HASH = namespace_hash(
    "schema", DIGEST_TIER0_PAYLOAD_SCHEMA_V1
)


class SemanticActivationError(RuntimeError):
    """A projection source or activation state failed closed."""


@dataclass(frozen=True)
class DigestProjectionCandidate:
    manifest: ProjectionManifestV2
    entry: ProjectionOutboxV2
    text: str
    payload: dict[str, Any]
    embedding_config: dict[str, Any]
    provenance_closure: dict[str, str]


@dataclass(frozen=True)
class DigestProjectionExclusion:
    cache_key: str
    job_id: str
    corpus_id: str
    doc_id: str
    parent_id: str
    faithfulness_status: str
    supersession_reason: str
    superseded_at: str
    superseded_by_cache_key: str
    successor_job_id: str

    def receipt(self) -> dict[str, str]:
        return {
            "cache_key": self.cache_key,
            "job_id": self.job_id,
            "corpus_id": self.corpus_id,
            "doc_id": self.doc_id,
            "parent_id": self.parent_id,
            "faithfulness_status": self.faithfulness_status,
            "supersession_reason": self.supersession_reason,
            "superseded_at": self.superseded_at,
            "superseded_by_cache_key": self.superseded_by_cache_key,
            "successor_job_id": self.successor_job_id,
        }


@dataclass(frozen=True)
class DigestProjectionSelection:
    candidates: tuple[DigestProjectionCandidate, ...]
    exclusions: tuple[DigestProjectionExclusion, ...]


def _terminal_quarantine_exclusion(
    cache: dict[str, Any], job: dict[str, Any]
) -> DigestProjectionExclusion | None:
    """Return a typed, auditable exclusion for a terminal digest quarantine."""

    status = str(cache.get("faithfulness_status") or "")
    reason = str(cache.get("supersession_reason") or "")
    superseded_at = cache.get("superseded_at")
    successor_cache_key = str(cache.get("superseded_by_cache_key") or "")
    if (
        cache.get("status") != "accepted_cache"
        or cache.get("canonical_write") is not False
        or cache.get("serving_eligible") is not False
        or (status, reason) not in TERMINAL_DIGEST_QUARANTINES
        or not isinstance(superseded_at, datetime)
        or not successor_cache_key
        or successor_cache_key == str(cache.get("_id") or "")
    ):
        return None
    return DigestProjectionExclusion(
        cache_key=str(cache.get("_id") or ""),
        job_id=str(job.get("job_id") or ""),
        corpus_id=str(job.get("corpus_id") or ""),
        doc_id=str(job.get("doc_id") or ""),
        parent_id=str(job.get("parent_id") or ""),
        faithfulness_status=status,
        supersession_reason=reason,
        superseded_at=_aware(superseded_at).isoformat(),
        superseded_by_cache_key=successor_cache_key,
        successor_job_id="",
    )


def _classify_digest_cache(
    cache: dict[str, Any], job: dict[str, Any]
) -> DigestProjectionExclusion | None:
    """Return ``None`` for serving rows or an accounted terminal exclusion."""

    if (
        cache.get("status") == "accepted_cache"
        and cache.get("canonical_write") is False
        and cache.get("serving_eligible") is not False
    ):
        return None
    exclusion = _terminal_quarantine_exclusion(cache, job)
    if exclusion is None:
        raise SemanticActivationError(
            "digest cache is non-serving without typed terminal quarantine: "
            + str(cache.get("_id") or "")
        )
    return exclusion


def _resolve_digest_cache_selection(
    *,
    cache_keys: list[str],
    raw_cache_by_key: dict[str, dict[str, Any]],
    jobs_by_cache: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], list[DigestProjectionExclusion]]:
    """Resolve serving winners and explicitly linked terminal predecessors."""

    cache_by_key: dict[str, dict[str, Any]] = {}
    preliminary: list[DigestProjectionExclusion] = []
    for cache_key in cache_keys:
        cache = raw_cache_by_key[cache_key]
        job = jobs_by_cache[cache_key][0]
        exclusion = _classify_digest_cache(cache, job)
        if exclusion is None:
            cache_by_key[cache_key] = cache
        else:
            preliminary.append(exclusion)

    eligible_by_owner: dict[tuple[str, str, str], list[str]] = {}
    for cache_key in sorted(cache_by_key):
        job = jobs_by_cache[cache_key][0]
        owner = (
            str(job.get("corpus_id") or ""),
            str(job.get("doc_id") or ""),
            str(job.get("parent_id") or ""),
        )
        eligible_by_owner.setdefault(owner, []).append(cache_key)
    ambiguous_eligible = sorted(
        "/".join(owner)
        for owner, values in eligible_by_owner.items()
        if len(values) != 1
    )
    if ambiguous_eligible:
        raise SemanticActivationError(
            "multiple serving-eligible digest winners for parent(s): "
            + ", ".join(ambiguous_eligible[:8])
        )

    exclusions: list[DigestProjectionExclusion] = []
    for exclusion in preliminary:
        predecessor_job = jobs_by_cache[exclusion.cache_key][0]
        predecessor_owner = (
            str(predecessor_job.get("corpus_id") or ""),
            str(predecessor_job.get("doc_id") or ""),
            str(predecessor_job.get("parent_id") or ""),
        )
        successor_key = exclusion.superseded_by_cache_key
        successor_cache = cache_by_key.get(successor_key)
        successor_jobs = jobs_by_cache.get(successor_key, [])
        if successor_cache is None or len(successor_jobs) != 1:
            raise SemanticActivationError(
                "digest quarantine successor is not one serving succeeded cache/job: "
                + exclusion.cache_key
            )
        successor_job = successor_jobs[0]
        successor_owner = (
            str(successor_job.get("corpus_id") or ""),
            str(successor_job.get("doc_id") or ""),
            str(successor_job.get("parent_id") or ""),
        )
        if successor_owner != predecessor_owner:
            raise SemanticActivationError(
                "digest quarantine successor ownership drifted: " + exclusion.cache_key
            )
        if (
            successor_cache.get("serving_eligible") is False
            or successor_cache.get("superseded_at") is not None
            or str(successor_cache.get("superseded_by_cache_key") or "")
        ):
            raise SemanticActivationError(
                "digest quarantine successor is itself superseded or quarantined: "
                + exclusion.cache_key
            )
        if eligible_by_owner.get(predecessor_owner) != [successor_key]:
            raise SemanticActivationError(
                "digest quarantine does not resolve to the sole eligible winner: "
                + exclusion.cache_key
            )
        exclusions.append(
            replace(
                exclusion,
                successor_job_id=str(successor_job.get("job_id") or ""),
            )
        )
    return cache_by_key, exclusions


def _job_prompt_version_closure(
    *,
    cache_key: str,
    job: dict[str, Any],
    provenance: SemanticGatewayProvenance,
) -> dict[str, str]:
    """Close omitted legacy labels only through exact cache provenance."""

    prompt_version = job.get("prompt_version")
    repair_prompt_version = job.get("repair_prompt_version")
    prompt_missing = prompt_version is None
    repair_missing = repair_prompt_version is None
    if prompt_missing != repair_missing:
        raise SemanticActivationError(
            "digest job prompt version labels are partially missing"
        )
    if not provenance.prompt_version or not provenance.repair_prompt_version:
        raise SemanticActivationError("digest cache prompt version labels are missing")
    if prompt_missing and repair_missing:
        mode = "legacy_missing_job_prompt_version_labels"
    else:
        if str(prompt_version) != provenance.prompt_version:
            raise SemanticActivationError("digest job prompt_version drifted")
        if str(repair_prompt_version) != provenance.repair_prompt_version:
            raise SemanticActivationError("digest job repair_prompt_version drifted")
        mode = "job_prompt_version_labels_exact"
    return {
        "mode": mode,
        "job_id": str(job.get("job_id") or ""),
        "cache_key": cache_key,
        "adopted_prompt_version": provenance.prompt_version,
        "adopted_repair_prompt_version": provenance.repair_prompt_version,
    }


def _aware(value: Any) -> Any:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    if isinstance(value, dict):
        return {key: _aware(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_aware(item) for item in value]
    return value


def _model_document(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="python")


def semantic_digest_text(digest: SemanticDigestV1) -> str:
    """The complete, frozen document-side representation (Qwen docs are raw)."""

    return f"{digest.summary.strip()}\n{digest.central_thesis.strip()}"


def semantic_digest_artifact_id(
    *, corpus_id: str, doc_id: str, source_version_id: str, parent_id: str
) -> str:
    digest = namespace_hash(
        "logical-artifact",
        {
            "artifact_kind": "semantic_digest",
            "natural_keys": {
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "source_version_id": source_version_id,
                "parent_id": parent_id,
            },
        },
    ).split(":", 1)[1]
    return f"semantic-digest:{digest}"


def _embedding_manifest(
    *, embedding_config: dict[str, Any], schema_hash: str
) -> ProjectionManifestV2:
    from services.embedder import query_embedding_profile
    from services.ingestion.tier0 import SHARED_DOCSUM

    settings = get_settings()
    model_id = str(
        embedding_config.get("embedding_model_id") or settings.EMBEDDER_MODEL_NAME
    )
    dims = int(
        embedding_config.get("embedding_dimension")
        or getattr(settings, "EMBEDDING_DIMENSION", 1024)
    )
    query_profile = query_embedding_profile(
        model_id=model_id,
        profile_name=str(
            embedding_config.get("query_instruction_profile")
            or settings.QWEN3_QUERY_INSTRUCTION_PROFILE
        ),
    )
    return make_activation_manifest(
        family="document_summary",
        representation_role=DIGEST_REPRESENTATION_ROLE,
        source_schema_hashes={"semantic_digest.v1": schema_hash},
        payload_schema_hash=DIGEST_TIER0_PAYLOAD_SCHEMA_HASH,
        embedding_profile=ActivationEmbeddingProfileV2(
            model_id=model_id,
            model_revision=str(
                embedding_config.get("embedding_model_revision") or model_id
            ),
            dims=dims,
            quantization=(
                "binary" if settings.QDRANT_BINARY_QUANTIZATION_ENABLED else "float32"
            ),
            instruction_version=str(query_profile["instruction_version"]),
            document_side_instruction="raw",
            sparse_recipe_version="none",
        ),
        search_compat=SearchCompat(
            oversampling=float(settings.QDRANT_BINARY_QUANTIZATION_OVERSAMPLING),
            rescore_with_full_vectors=bool(settings.QDRANT_BINARY_QUANTIZATION_RESCORE),
            distance="cosine",
        ),
        target=ProjectionTargetV2(
            collection_name=SHARED_DOCSUM,
            vector_name="dense",
        ),
        recipe_version=DIGEST_RECIPE_VERSION,
        rollback_predecessor=None,
    )


def _validate_source_rows(
    *,
    cache: dict[str, Any],
    job: dict[str, Any],
    parent: dict[str, Any],
    document: dict[str, Any],
    children: list[dict[str, Any]],
    embedding_config: dict[str, Any],
) -> DigestProjectionCandidate:
    from services.ingestion.semantic_digest_claim_inputs import (
        document_source_version_id,
    )

    if cache.get("status") != "accepted_cache":
        raise SemanticActivationError("digest cache is not accepted")
    if cache.get("serving_eligible") is False:
        raise SemanticActivationError("digest cache is not serving eligible")
    if cache.get("canonical_write") is not False:
        raise SemanticActivationError("digest cache canonical-write boundary drifted")
    digest = SemanticDigestV1.model_validate(cache.get("digest"))
    provenance = SemanticGatewayProvenance.model_validate(cache.get("provenance"))
    cache_key = str(cache.get("_id") or "")
    if not cache_key or provenance.cache_key != cache_key:
        raise SemanticActivationError("digest cache identity does not close")
    if job.get("status") != "succeeded":
        raise SemanticActivationError("digest ownership job is not succeeded")
    job_id = str(job.get("job_id") or "")
    corpus_id = str(job.get("corpus_id") or "")
    doc_id = str(job.get("doc_id") or "")
    parent_id = str(job.get("parent_id") or "")
    if not all((job_id, corpus_id, doc_id, parent_id)):
        raise SemanticActivationError("digest ownership job is incomplete")
    if str(job.get("cache_key") or "") != cache_key:
        raise SemanticActivationError("digest job/cache identity does not close")
    provenance_closure = _job_prompt_version_closure(
        cache_key=cache_key,
        job=job,
        provenance=provenance,
    )
    if digest.parent_id != parent_id:
        raise SemanticActivationError("digest parent does not match its ownership job")
    for field, expected in (
        ("input_hash", provenance.input_hash),
        ("output_hash", provenance.output_hash),
        ("schema_hash", provenance.schema_hash),
        ("prompt_hash", provenance.prompt_hash),
        ("repair_prompt_hash", provenance.repair_prompt_hash),
        ("model_id", provenance.model_id),
        ("runtime_version", provenance.runtime_version),
    ):
        actual = job.get(field)
        if actual is None or str(actual) != expected:
            raise SemanticActivationError(f"digest job {field} drifted")
    if provenance.schema_hash != semantic_digest_schema_hash():
        raise SemanticActivationError("digest schema hash is not current")
    if provenance.prompt_hash != semantic_digest_prompt_hash(
        provenance.prompt_version, provenance.repair_prompt_version
    ):
        raise SemanticActivationError("digest prompt hash is not reproducible")
    if provenance.repair_prompt_hash != semantic_digest_repair_prompt_hash(
        provenance.repair_prompt_version
    ):
        raise SemanticActivationError("digest repair prompt hash is not reproducible")
    if cache_key != semantic_digest_cache_key(
        input_hash=provenance.input_hash,
        model_id=provenance.model_id,
        schema_hash=provenance.schema_hash,
        prompt_hash=provenance.prompt_hash,
        runtime_version=provenance.runtime_version,
    ):
        raise SemanticActivationError("digest cache key is not reproducible")
    if (
        str(parent.get("corpus_id") or "") != corpus_id
        or str(parent.get("doc_id") or "") != doc_id
        or str(parent.get("parent_id") or "") != parent_id
    ):
        raise SemanticActivationError("current parent ownership does not close")
    if (
        str(document.get("corpus_id") or "") != corpus_id
        or str(document.get("doc_id") or "") != doc_id
    ):
        raise SemanticActivationError("current document ownership does not close")
    if parent.get("validation_status") != "valid":
        raise SemanticActivationError("current parent is not validation_status=valid")
    parent_text = parent.get("text")
    if not isinstance(parent_text, str) or not parent_text.strip():
        raise SemanticActivationError("current parent text is missing")
    raw_parent_hash = hashlib.sha256(parent_text.encode("utf-8")).hexdigest()
    stored_parent_hash = str(parent.get("source_hash") or "").removeprefix("sha256:")
    if stored_parent_hash != raw_parent_hash:
        raise SemanticActivationError("current parent source hash drifted")
    parent_text_hash = namespace_hash("normalized-text", parent_text)
    source_child_ids = [
        str(value) for value in (parent.get("child_ids") or []) if str(value)
    ]
    if not source_child_ids or len(source_child_ids) != len(set(source_child_ids)):
        raise SemanticActivationError("current parent child identity set is invalid")
    child_ids = sorted(str(row.get("chunk_id") or "") for row in children)
    if sorted(source_child_ids) != child_ids:
        raise SemanticActivationError("current parent evidence child set drifted")
    for child in children:
        if (
            str(child.get("corpus_id") or "") != corpus_id
            or str(child.get("doc_id") or "") != doc_id
            or str(child.get("parent_id") or "") != parent_id
            or not isinstance(child.get("text"), str)
        ):
            raise SemanticActivationError("current parent evidence ownership drifted")
    source_child_ids_hash = namespace_hash("evidence-set", frozenset(source_child_ids))
    source_version = document_source_version_id(document)
    body_hash = namespace_hash("body", digest.model_dump(mode="python"))
    if body_hash != provenance.output_hash:
        raise SemanticActivationError("digest output hash does not match its body")
    artifact_id = semantic_digest_artifact_id(
        corpus_id=corpus_id,
        doc_id=doc_id,
        source_version_id=source_version,
        parent_id=parent_id,
    )
    revision_id = artifact_revision_id(
        artifact_id,
        provenance.schema_hash,
        body_hash,
    )
    manifest = _embedding_manifest(
        embedding_config=embedding_config,
        schema_hash=provenance.schema_hash,
    )
    point_id = projection_point_id(
        artifact_id,
        DIGEST_REPRESENTATION_ROLE,
        manifest.projection_profile_hash,
    )
    source = ProjectionSourceLocatorV2(
        source_kind="semantic_digest_cache",
        source_collection=DIGEST_CACHE_COLLECTION,
        source_id=cache_key,
        ownership_collection=DIGEST_JOB_COLLECTION,
        ownership_id=job_id,
        artifact_id=artifact_id,
        corpus_id=corpus_id,
        doc_id=doc_id,
        source_version_id=source_version,
        parent_id=parent_id,
        parent_text_hash=parent_text_hash,
        source_child_ids_hash=source_child_ids_hash,
        source_child_count=len(source_child_ids),
    )
    text = semantic_digest_text(digest)
    payload_without_hash = {
        "payload_schema_version": "semantic_digest_tier0_payload.v1",
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "parent_id": parent_id,
        "chunk_type": "semantic_digest",
        "summary": text,
        "concepts": [],
        "section_ids": [parent_id],
        "artifact_id": artifact_id,
        "artifact_revision_id": revision_id,
        "projection_role": DIGEST_REPRESENTATION_ROLE,
        "projection_manifest_id": manifest.manifest_id,
        "projection_profile_hash": manifest.projection_profile_hash,
        "source_cache_key": cache_key,
        "source_job_id": job_id,
        "source_version_id": source_version,
        "model_id": provenance.model_id,
        "schema_version": provenance.schema_version,
        "schema_hash": provenance.schema_hash,
        "prompt_version": provenance.prompt_version,
        "prompt_hash": provenance.prompt_hash,
        "output_hash": provenance.output_hash,
        "provenance_closure": provenance_closure,
    }
    projected_payload_hash = namespace_hash("body", payload_without_hash)
    payload = {
        **payload_without_hash,
        "projected_payload_hash": projected_payload_hash,
    }
    now = datetime.now(timezone.utc)
    entry = make_activation_entry(
        artifact_revision_id=revision_id,
        manifest_id=manifest.manifest_id,
        point_id=point_id,
        projected_payload_hash=projected_payload_hash,
        source=source,
        now=now,
    )
    return DigestProjectionCandidate(
        manifest=manifest,
        entry=entry,
        text=text,
        payload=payload,
        embedding_config=dict(embedding_config),
        provenance_closure=provenance_closure,
    )


async def discover_digest_projection_selection(
    db: Any,
    *,
    corpus_ids: Iterable[str] | None = None,
) -> DigestProjectionSelection:
    """Read serving digests and account for typed terminal quarantines."""

    from services.storage.record_status import with_active_records

    requested = sorted({str(value) for value in (corpus_ids or []) if str(value)})
    job_query: dict[str, Any] = {
        "status": "succeeded",
    }
    if requested:
        job_query["corpus_id"] = {"$in": requested}
    jobs = await db[DIGEST_JOB_COLLECTION].find(job_query).to_list(length=None)
    missing_job_cache_key = sorted(
        str(row.get("job_id") or "")
        for row in jobs
        if not str(row.get("cache_key") or "")
    )
    if missing_job_cache_key:
        raise SemanticActivationError(
            "succeeded digest job is missing cache identity: "
            + ", ".join(missing_job_cache_key[:8])
        )
    cache_keys = sorted({str(row.get("cache_key") or "") for row in jobs} - {""})
    jobs_by_cache: dict[str, list[dict[str, Any]]] = {}
    for row in jobs:
        jobs_by_cache.setdefault(str(row.get("cache_key") or ""), []).append(row)
    ambiguous_jobs = sorted(
        key for key, values in jobs_by_cache.items() if len(values) != 1
    )
    if ambiguous_jobs:
        raise SemanticActivationError(
            "digest ownership job is ambiguous: " + ", ".join(ambiguous_jobs[:8])
        )
    cache_rows = (
        await db[DIGEST_CACHE_COLLECTION]
        .find({"_id": {"$in": cache_keys}})
        .to_list(length=None)
    )
    raw_cache_by_key = {str(row.get("_id") or ""): row for row in cache_rows}
    missing_cache = sorted(set(cache_keys) - set(raw_cache_by_key))
    if missing_cache:
        raise SemanticActivationError(
            "succeeded digest job is missing cache row: " + ", ".join(missing_cache[:8])
        )
    cache_by_key, exclusions = _resolve_digest_cache_selection(
        cache_keys=cache_keys,
        raw_cache_by_key=raw_cache_by_key,
        jobs_by_cache=jobs_by_cache,
    )
    selected_jobs = [jobs_by_cache[key][0] for key in sorted(cache_by_key)]
    parent_ids = sorted({str(row.get("parent_id") or "") for row in selected_jobs})
    doc_ids = sorted({str(row.get("doc_id") or "") for row in selected_jobs})
    selected_corpora = sorted(
        {str(row.get("corpus_id") or "") for row in selected_jobs}
    )
    parents = (
        await db["parent_chunks"]
        .find(
            with_active_records(
                {
                    "parent_id": {"$in": parent_ids},
                    "corpus_id": {"$in": selected_corpora},
                }
            )
        )
        .to_list(length=None)
    )
    documents = (
        await db["documents"]
        .find(
            with_active_records(
                {"doc_id": {"$in": doc_ids}, "corpus_id": {"$in": selected_corpora}}
            )
        )
        .to_list(length=None)
    )
    corpora = (
        await db["corpora"]
        .find(
            with_active_records({"corpus_id": {"$in": selected_corpora}}),
            {"_id": 0, "corpus_id": 1, "default_ingestion_config": 1},
        )
        .to_list(length=None)
    )
    child_ids = sorted(
        {
            str(child_id)
            for parent in parents
            for child_id in (parent.get("child_ids") or [])
            if str(child_id)
        }
    )
    children = (
        await db["chunks"]
        .find(
            with_active_records(
                {"chunk_id": {"$in": child_ids}, "corpus_id": {"$in": selected_corpora}}
            ),
            {
                "_id": 0,
                "corpus_id": 1,
                "doc_id": 1,
                "parent_id": 1,
                "chunk_id": 1,
                "text": 1,
            },
        )
        .to_list(length=None)
    )

    def _unique_map(rows, key_fn, label):
        grouped: dict[Any, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(key_fn(row), []).append(row)
        duplicate = [key for key, values in grouped.items() if len(values) != 1]
        if duplicate:
            raise SemanticActivationError(
                f"duplicate active {label} ownership: {duplicate[:4]}"
            )
        return {key: values[0] for key, values in grouped.items()}

    parent_map = _unique_map(
        parents,
        lambda row: (str(row.get("corpus_id") or ""), str(row.get("parent_id") or "")),
        "parent",
    )
    document_map = _unique_map(
        documents,
        lambda row: (str(row.get("corpus_id") or ""), str(row.get("doc_id") or "")),
        "document",
    )
    config_rows = _unique_map(
        corpora, lambda row: str(row.get("corpus_id") or ""), "corpus"
    )
    config_map = {
        key: dict(row.get("default_ingestion_config") or {})
        for key, row in config_rows.items()
    }
    children_by_parent: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for child in children:
        key = (
            str(child.get("corpus_id") or ""),
            str(child.get("doc_id") or ""),
            str(child.get("parent_id") or ""),
        )
        children_by_parent.setdefault(key, []).append(child)
    candidates: list[DigestProjectionCandidate] = []
    for job in sorted(
        selected_jobs,
        key=lambda row: (
            str(row.get("corpus_id") or ""),
            str(row.get("doc_id") or ""),
            str(row.get("parent_id") or ""),
        ),
    ):
        corpus_id = str(job.get("corpus_id") or "")
        doc_id = str(job.get("doc_id") or "")
        parent_id = str(job.get("parent_id") or "")
        cache_key = str(job.get("cache_key") or "")
        cache = cache_by_key.get(cache_key)
        parent = parent_map.get((corpus_id, parent_id))
        document = document_map.get((corpus_id, doc_id))
        config = config_map.get(corpus_id)
        if cache is None or parent is None or document is None or config is None:
            raise SemanticActivationError(
                f"digest source closure missing for {corpus_id}/{doc_id}/{parent_id}"
            )
        candidates.append(
            _validate_source_rows(
                cache=cache,
                job=job,
                parent=parent,
                document=document,
                children=children_by_parent.get((corpus_id, doc_id, parent_id), []),
                embedding_config=config,
            )
        )
    return DigestProjectionSelection(
        candidates=tuple(candidates),
        exclusions=tuple(sorted(exclusions, key=lambda row: row.cache_key)),
    )


async def discover_digest_projection_candidates(
    db: Any,
    *,
    corpus_ids: Iterable[str] | None = None,
) -> list[DigestProjectionCandidate]:
    """Compatibility wrapper returning only eligible projection candidates."""

    selection = await discover_digest_projection_selection(db, corpus_ids=corpus_ids)
    return list(selection.candidates)


class ProjectionActivationRepository:
    def __init__(self, db: Any) -> None:
        self.db = db

    async def ensure_indexes(self) -> None:
        await self.db[MANIFEST_COLLECTION].create_index(
            [("manifest_id", ASCENDING)],
            unique=True,
            name="manifest_id_unique_v2",
            partialFilterExpression={"schema_version": "projection_manifest.v2"},
        )
        await self.db[OUTBOX_COLLECTION].create_index(
            [("outbox_id", ASCENDING)],
            unique=True,
            name="outbox_id_unique_v2",
            partialFilterExpression={"schema_version": "projection_outbox.v2"},
        )
        await self.db[OUTBOX_COLLECTION].create_index(
            [
                ("schema_version", ASCENDING),
                ("state", ASCENDING),
                ("lease_expires_at", ASCENDING),
            ],
            name="projection_activation_claim_v2",
        )

    async def save_manifest(self, manifest: ProjectionManifestV2) -> None:
        await self.db[MANIFEST_COLLECTION].update_one(
            {"manifest_id": manifest.manifest_id},
            {"$setOnInsert": _model_document(manifest)},
            upsert=True,
        )
        stored = await self.db[MANIFEST_COLLECTION].find_one(
            {"manifest_id": manifest.manifest_id}, {"_id": 0}
        )
        if stored is None or ProjectionManifestV2.model_validate(stored) != manifest:
            raise SemanticActivationError("immutable projection manifest collision")

    async def enqueue(self, entry: ProjectionOutboxV2) -> None:
        await self.db[OUTBOX_COLLECTION].update_one(
            {"outbox_id": entry.outbox_id},
            {"$setOnInsert": _model_document(entry)},
            upsert=True,
        )
        stored = await self.db[OUTBOX_COLLECTION].find_one(
            {"outbox_id": entry.outbox_id}, {"_id": 0}
        )
        if stored is None:
            raise SemanticActivationError("projection outbox insert disappeared")
        parsed = ProjectionOutboxV2.model_validate(_aware(stored))
        immutable = {
            "outbox_id",
            "artifact_revision_id",
            "manifest_id",
            "point_id",
            "projected_payload_hash",
            "op",
            "source",
            "max_attempts",
        }
        if any(getattr(parsed, field) != getattr(entry, field) for field in immutable):
            raise SemanticActivationError("immutable projection outbox collision")

    async def load_manifest(self, manifest_id: str) -> ProjectionManifestV2:
        row = await self.db[MANIFEST_COLLECTION].find_one(
            {"manifest_id": manifest_id}, {"_id": 0}
        )
        if row is None:
            raise SemanticActivationError("projection manifest is missing")
        return ProjectionManifestV2.model_validate(row)

    async def claim_reconciliation_one(
        self,
        *,
        owner: str,
        now: datetime,
        lease_seconds: int = 300,
        corpus_ids: Iterable[str] | None = None,
    ) -> ProjectionOutboxV2 | None:
        expires = now + timedelta(seconds=max(30, int(lease_seconds)))
        scope = sorted({str(value) for value in (corpus_ids or []) if str(value)})
        scope_clause = {"source.corpus_id": {"$in": scope}} if scope else {}
        # Exhausted expired rows get a reconciliation-only lease without
        # incrementing the attempt budget. The worker must first check whether
        # the exact payload already reached Qdrant before it may dead-letter.
        row = await self.db[OUTBOX_COLLECTION].find_one_and_update(
            {
                "schema_version": "projection_outbox.v2",
                "state": "in_flight",
                "lease_expires_at": {"$lte": now},
                "$expr": {"$gte": ["$attempt_count", "$max_attempts"]},
                **scope_clause,
            },
            {
                "$set": {
                    "lease_owner": owner,
                    "lease_expires_at": expires,
                    "updated_at": now,
                },
                "$unset": {
                    "applied_at": "",
                    "application_receipt": "",
                    "last_error": "",
                },
            },
            sort=[("created_at", ASCENDING), ("outbox_id", ASCENDING)],
            return_document=ReturnDocument.AFTER,
            projection={"_id": 0},
        )
        return ProjectionOutboxV2.model_validate(_aware(row)) if row else None

    async def claim_one(
        self,
        *,
        owner: str,
        now: datetime,
        lease_seconds: int = 300,
        corpus_ids: Iterable[str] | None = None,
    ) -> ProjectionOutboxV2 | None:
        expires = now + timedelta(seconds=max(30, int(lease_seconds)))
        scope = sorted({str(value) for value in (corpus_ids or []) if str(value)})
        scope_clause = {"source.corpus_id": {"$in": scope}} if scope else {}
        row = await self.db[OUTBOX_COLLECTION].find_one_and_update(
            {
                "schema_version": "projection_outbox.v2",
                "$expr": {"$lt": ["$attempt_count", "$max_attempts"]},
                **scope_clause,
                "$or": [
                    {"state": {"$in": ["pending", "failed"]}},
                    {"state": "in_flight", "lease_expires_at": {"$lte": now}},
                ],
            },
            {
                "$set": {
                    "state": "in_flight",
                    "lease_owner": owner,
                    "lease_expires_at": expires,
                    "updated_at": now,
                },
                "$unset": {
                    "last_error": "",
                    "applied_at": "",
                    "application_receipt": "",
                },
                "$inc": {"attempt_count": 1},
            },
            sort=[("created_at", ASCENDING), ("outbox_id", ASCENDING)],
            return_document=ReturnDocument.AFTER,
            projection={"_id": 0},
        )
        return ProjectionOutboxV2.model_validate(_aware(row)) if row else None

    async def renew_lease(
        self,
        entry: ProjectionOutboxV2,
        *,
        owner: str,
        now: datetime,
        lease_seconds: int,
    ) -> None:
        result = await self.db[OUTBOX_COLLECTION].update_one(
            {
                "outbox_id": entry.outbox_id,
                "state": "in_flight",
                "lease_owner": owner,
                "attempt_count": entry.attempt_count,
            },
            {
                "$set": {
                    "updated_at": now,
                    "lease_expires_at": now
                    + timedelta(seconds=max(30, int(lease_seconds))),
                }
            },
        )
        if int(getattr(result, "modified_count", 0) or 0) != 1:
            raise SemanticActivationError("lost projection lease before renewal")

    async def mark_applied(
        self,
        entry: ProjectionOutboxV2,
        *,
        owner: str,
        now: datetime,
        receipt: ProjectionApplicationReceiptV2,
    ) -> None:
        if (
            receipt.point_id != entry.point_id
            or receipt.projected_payload_hash != entry.projected_payload_hash
        ):
            raise SemanticActivationError("projection application receipt drifted")
        result = await self.db[OUTBOX_COLLECTION].update_one(
            {
                "outbox_id": entry.outbox_id,
                "state": "in_flight",
                "lease_owner": owner,
                "attempt_count": entry.attempt_count,
            },
            {
                "$set": {
                    "state": "applied",
                    "updated_at": now,
                    "applied_at": now,
                    "application_receipt": receipt.model_dump(mode="python"),
                },
                "$unset": {
                    "lease_owner": "",
                    "lease_expires_at": "",
                    "last_error": "",
                },
            },
        )
        if int(getattr(result, "modified_count", 0) or 0) != 1:
            raise SemanticActivationError("lost projection lease before apply receipt")

    async def mark_failed(
        self,
        entry: ProjectionOutboxV2,
        *,
        owner: str,
        now: datetime,
        error: str,
    ) -> None:
        terminal = entry.attempt_count >= entry.max_attempts
        result = await self.db[OUTBOX_COLLECTION].update_one(
            {
                "outbox_id": entry.outbox_id,
                "state": "in_flight",
                "lease_owner": owner,
                "attempt_count": entry.attempt_count,
            },
            {
                "$set": {
                    "state": "dead" if terminal else "failed",
                    "updated_at": now,
                    "last_error": str(error)[:1000],
                },
                "$unset": {
                    "lease_owner": "",
                    "lease_expires_at": "",
                    "applied_at": "",
                    "application_receipt": "",
                },
            },
        )
        if int(getattr(result, "modified_count", 0) or 0) != 1:
            raise SemanticActivationError(
                "lost projection lease before failure receipt"
            )


async def ensure_activation_contracts(db: Any) -> dict[str, str]:
    """Apply only the two additive projection validators and v2 indexes."""

    from services.storage.schema_validators import (
        PROJECTION_MANIFESTS_SCHEMA,
        PROJECTION_OUTBOX_SCHEMA,
    )

    results: dict[str, str] = {}
    for collection_name, validator in (
        (MANIFEST_COLLECTION, PROJECTION_MANIFESTS_SCHEMA),
        (OUTBOX_COLLECTION, PROJECTION_OUTBOX_SCHEMA),
    ):
        command = {
            "collMod": collection_name,
            "validator": validator,
            "validationAction": "warn",
            "validationLevel": "moderate",
        }
        try:
            await db.command(command)
            results[collection_name] = "validator_applied"
        except Exception:
            existing = set(await db.list_collection_names())
            if collection_name in existing:
                raise
            await db.create_collection(
                collection_name,
                validator=validator,
                validationAction="warn",
                validationLevel="moderate",
            )
            results[collection_name] = "collection_created"
    await ProjectionActivationRepository(db).ensure_indexes()
    return results


async def enqueue_digest_projections(
    db: Any,
    *,
    corpus_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    selection = await discover_digest_projection_selection(db, corpus_ids=corpus_ids)
    candidates = list(selection.candidates)
    repository = ProjectionActivationRepository(db)
    await repository.ensure_indexes()
    for candidate in candidates:
        await repository.save_manifest(candidate.manifest)
        await repository.enqueue(candidate.entry)
    return {
        "candidate_count": len(candidates),
        "manifest_ids": sorted({row.manifest.manifest_id for row in candidates}),
        "outbox_ids": [row.entry.outbox_id for row in candidates],
        "point_ids": [row.entry.point_id for row in candidates],
        "legacy_provenance_closure_count": sum(
            row.provenance_closure["mode"] == "legacy_missing_job_prompt_version_labels"
            for row in candidates
        ),
        "legacy_provenance_closures": [
            row.provenance_closure
            for row in candidates
            if row.provenance_closure["mode"]
            == "legacy_missing_job_prompt_version_labels"
        ],
        "excluded_count": len(selection.exclusions),
        "exclusions": [row.receipt() for row in selection.exclusions],
    }


def _bind_validated_candidate_to_claim(
    candidate: DigestProjectionCandidate,
    claimed: ProjectionOutboxV2,
) -> DigestProjectionCandidate:
    """Preserve the durable attempt/lease after rebuilding immutable source data."""

    if candidate.entry.source != claimed.source:
        raise SemanticActivationError("projection source ownership drifted")
    if candidate.entry.artifact_revision_id != claimed.artifact_revision_id:
        raise SemanticActivationError("projection artifact revision drifted")
    if candidate.entry.point_id != claimed.point_id:
        raise SemanticActivationError("projection point identity drifted")
    if candidate.entry.manifest_id != claimed.manifest_id:
        raise SemanticActivationError("projection profile drifted")
    if candidate.entry.projected_payload_hash != claimed.projected_payload_hash:
        raise SemanticActivationError("projection payload identity drifted")
    return replace(candidate, entry=claimed)


class SemanticDigestProjectionWorker:
    def __init__(
        self,
        db: Any,
        qdrant_client: Any,
        *,
        owner: str,
        corpus_ids: Iterable[str] | None = None,
    ) -> None:
        self.db = db
        self.qdrant_client = qdrant_client
        self.owner = owner
        self.corpus_ids = tuple(
            sorted({str(value) for value in (corpus_ids or []) if str(value)})
        )
        self.repository = ProjectionActivationRepository(db)

    async def _candidate_for_entry(
        self, entry: ProjectionOutboxV2
    ) -> DigestProjectionCandidate:
        from services.storage.record_status import with_active_records

        source = entry.source
        cache_rows = (
            await self.db[source.source_collection]
            .find({"_id": source.source_id})
            .to_list(length=2)
        )
        job_rows = (
            await self.db[source.ownership_collection]
            .find({"job_id": source.ownership_id}, {"_id": 0})
            .to_list(length=2)
        )
        parent_rows = (
            await self.db["parent_chunks"]
            .find(
                with_active_records(
                    {"corpus_id": source.corpus_id, "parent_id": source.parent_id}
                )
            )
            .to_list(length=2)
        )
        document_rows = (
            await self.db["documents"]
            .find(
                with_active_records(
                    {"corpus_id": source.corpus_id, "doc_id": source.doc_id}
                )
            )
            .to_list(length=2)
        )
        corpus_rows = (
            await self.db["corpora"]
            .find(
                with_active_records({"corpus_id": source.corpus_id}),
                {"_id": 0, "default_ingestion_config": 1},
            )
            .to_list(length=2)
        )
        for label, values in (
            ("cache", cache_rows),
            ("job", job_rows),
            ("parent", parent_rows),
            ("document", document_rows),
            ("corpus", corpus_rows),
        ):
            if len(values) != 1:
                raise SemanticActivationError(
                    f"projection {label} ownership is missing or ambiguous"
                )
        cache, job, parent, document, corpus = (
            cache_rows[0],
            job_rows[0],
            parent_rows[0],
            document_rows[0],
            corpus_rows[0],
        )
        child_ids = [
            str(value) for value in (parent.get("child_ids") or []) if str(value)
        ]
        children = (
            await self.db["chunks"]
            .find(
                with_active_records(
                    {
                        "corpus_id": source.corpus_id,
                        "doc_id": source.doc_id,
                        "parent_id": source.parent_id,
                        "chunk_id": {"$in": child_ids},
                    }
                ),
                {
                    "_id": 0,
                    "corpus_id": 1,
                    "doc_id": 1,
                    "parent_id": 1,
                    "chunk_id": 1,
                    "text": 1,
                },
            )
            .to_list(length=max(2, len(child_ids) + 1))
        )
        candidate = _validate_source_rows(
            cache=cache,
            job=job,
            parent=parent,
            document=document,
            children=children,
            embedding_config=dict(corpus.get("default_ingestion_config") or {}),
        )
        candidate = _bind_validated_candidate_to_claim(candidate, entry)
        stored_manifest = await self.repository.load_manifest(entry.manifest_id)
        if stored_manifest != candidate.manifest:
            raise SemanticActivationError("stored projection manifest drifted")
        return candidate

    async def _reconcile_existing_application(
        self,
        entry: ProjectionOutboxV2,
    ) -> ProjectionApplicationReceiptV2 | None:
        """Return a receipt only when the exact exhausted payload is present."""

        manifest = await self.repository.load_manifest(entry.manifest_id)
        rows = await self.qdrant_client.retrieve(
            collection_name=manifest.target.collection_name,
            ids=[entry.point_id],
            with_payload=True,
            with_vectors=False,
        )
        if len(rows) != 1 or str(rows[0].id) != entry.point_id:
            return None
        payload = dict(getattr(rows[0], "payload", None) or {})
        payload_hash = str(payload.pop("projected_payload_hash", ""))
        if (
            payload_hash != entry.projected_payload_hash
            or namespace_hash("body", payload) != entry.projected_payload_hash
        ):
            return None
        now = datetime.now(timezone.utc)
        return ProjectionApplicationReceiptV2(
            target_collection=manifest.target.collection_name,
            vector_name=manifest.target.vector_name,
            point_id=entry.point_id,
            projected_payload_hash=entry.projected_payload_hash,
            operation_id=None,
            applied_at=now,
            reconciled=True,
        )

    async def _record_reconciled_if_present(
        self,
        entry: ProjectionOutboxV2,
        counts: dict[str, int],
    ) -> Literal["applied", "absent", "pending"]:
        try:
            receipt = await self._reconcile_existing_application(entry)
        except Exception:
            # An unavailable store/manifest is not proof the point was absent.
            counts["ack_pending"] += 1
            return "pending"
        if receipt is None:
            return "absent"
        try:
            await self.repository.mark_applied(
                entry,
                owner=self.owner,
                now=receipt.applied_at,
                receipt=receipt,
            )
            counts["applied"] += 1
            counts["reconciled"] += 1
            return "applied"
        except Exception:
            counts["ack_pending"] += 1
            return "pending"

    async def drain_batch(
        self,
        *,
        limit: int = 32,
        reconciliation_only: bool = False,
    ) -> dict[str, int]:
        from services.embedder import embed_documents
        from services.ingestion.tier0 import _ensure_collection

        max_batch = min(max(1, int(limit)), 32)
        lease_seconds = 900
        await self.repository.ensure_indexes()
        claimed: list[tuple[ProjectionOutboxV2, bool]] = []
        for _ in range(max_batch):
            entry = await self.repository.claim_reconciliation_one(
                owner=self.owner,
                now=datetime.now(timezone.utc),
                lease_seconds=lease_seconds,
                corpus_ids=self.corpus_ids,
            )
            is_reconciliation = entry is not None
            if entry is None and not reconciliation_only:
                entry = await self.repository.claim_one(
                    owner=self.owner,
                    now=datetime.now(timezone.utc),
                    lease_seconds=lease_seconds,
                    corpus_ids=self.corpus_ids,
                )
            if entry is None:
                break
            claimed.append((entry, is_reconciliation))
        counts = {
            "claimed": len(claimed),
            "applied": 0,
            "reconciled": 0,
            "failed": 0,
            "dead": 0,
            "ack_pending": 0,
        }
        if not claimed:
            return counts

        candidates: list[DigestProjectionCandidate] = []
        for entry, is_reconciliation in claimed:
            if is_reconciliation:
                status = await self._record_reconciled_if_present(entry, counts)
                if status != "absent":
                    continue
                try:
                    await self.repository.mark_failed(
                        entry,
                        owner=self.owner,
                        now=datetime.now(timezone.utc),
                        error="exhausted projection is absent or payload-mismatched in Qdrant",
                    )
                    counts["dead"] += 1
                except Exception:
                    counts["ack_pending"] += 1
                continue
            try:
                if entry.op != "upsert":
                    raise SemanticActivationError(
                        "live digest activation permits upsert only"
                    )
                candidates.append(await self._candidate_for_entry(entry))
            except Exception as exc:  # source-specific fail-closed receipt
                try:
                    await self.repository.mark_failed(
                        entry,
                        owner=self.owner,
                        now=datetime.now(timezone.utc),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    counts[
                        "dead"
                        if entry.attempt_count >= entry.max_attempts
                        else "failed"
                    ] += 1
                except Exception:
                    counts["ack_pending"] += 1

        grouped: dict[tuple[str, str], list[DigestProjectionCandidate]] = {}
        for candidate in candidates:
            grouped.setdefault(
                (candidate.manifest.manifest_id, candidate.entry.source.corpus_id), []
            ).append(candidate)
        for _group_key, rows in grouped.items():
            manifest = rows[0].manifest
            operation_ids: list[str] = []
            try:
                for row in rows:
                    await self.repository.renew_lease(
                        row.entry,
                        owner=self.owner,
                        now=datetime.now(timezone.utc),
                        lease_seconds=lease_seconds,
                    )
                await _ensure_collection(
                    self.qdrant_client, manifest.embedding_profile.dims
                )
                vectors = await embed_documents(
                    [row.text for row in rows],
                    rows[0].embedding_config,
                    workload_class="document_ingestion",
                )
                if len(vectors) != len(rows) or any(
                    len(vector) != manifest.embedding_profile.dims for vector in vectors
                ):
                    raise SemanticActivationError("digest embedding dimension drifted")
                points = [
                    qm.PointStruct(
                        id=row.entry.point_id,
                        vector={manifest.target.vector_name: vector},
                        payload=row.payload,
                    )
                    for row, vector in zip(rows, vectors, strict=True)
                ]
                batch_size = max(1, int(get_settings().QDRANT_UPSERT_BATCH_SIZE))
                for start in range(0, len(points), batch_size):
                    result = await self.qdrant_client.upsert(
                        collection_name=manifest.target.collection_name,
                        points=points[start : start + batch_size],
                        wait=True,
                    )
                    operation_id = getattr(result, "operation_id", None)
                    if operation_id is not None:
                        operation_ids.append(str(operation_id))

                readback = await self.qdrant_client.retrieve(
                    collection_name=manifest.target.collection_name,
                    ids=[row.entry.point_id for row in rows],
                    with_payload=True,
                    with_vectors=False,
                )
                readback_by_id = {str(point.id): point for point in readback}
                for row in rows:
                    point = readback_by_id.get(str(row.entry.point_id))
                    payload = dict(getattr(point, "payload", None) or {})
                    payload_hash = str(payload.pop("projected_payload_hash", ""))
                    if (
                        point is None
                        or payload_hash != row.entry.projected_payload_hash
                        or namespace_hash("body", payload)
                        != row.entry.projected_payload_hash
                    ):
                        raise SemanticActivationError(
                            "Qdrant projection payload reconciliation failed"
                        )
                for row in rows:
                    applied_at = datetime.now(timezone.utc)
                    receipt = ProjectionApplicationReceiptV2(
                        target_collection=manifest.target.collection_name,
                        vector_name=manifest.target.vector_name,
                        point_id=row.entry.point_id,
                        projected_payload_hash=row.entry.projected_payload_hash,
                        operation_id=operation_ids[-1] if operation_ids else None,
                        applied_at=applied_at,
                        reconciled=True,
                    )
                    try:
                        await self.repository.mark_applied(
                            row.entry,
                            owner=self.owner,
                            now=applied_at,
                            receipt=receipt,
                        )
                        counts["applied"] += 1
                    except Exception:
                        # The point is already reconciled. Leave the durable
                        # lease for idempotent reclaim; never regress it after
                        # a successful store application.
                        counts["ack_pending"] += 1
            except Exception as exc:
                for row in rows:
                    if row.entry.attempt_count >= row.entry.max_attempts:
                        status = await self._record_reconciled_if_present(
                            row.entry, counts
                        )
                        if status != "absent":
                            continue
                    try:
                        await self.repository.mark_failed(
                            row.entry,
                            owner=self.owner,
                            now=datetime.now(timezone.utc),
                            error=f"{type(exc).__name__}: {exc}",
                        )
                        counts[
                            "dead"
                            if row.entry.attempt_count >= row.entry.max_attempts
                            else "failed"
                        ] += 1
                    except Exception:
                        counts["ack_pending"] += 1
        return counts

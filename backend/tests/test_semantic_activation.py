from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from models.hash_taxonomy import namespace_hash
from models.identifier_recipes import projection_point_id
from models.projection_activation import (
    ActivationEmbeddingProfileV2,
    ProjectionManifestV2,
    ProjectionOutboxV2,
    ProjectionSourceLocatorV2,
    ProjectionTargetV2,
    activation_outbox_id,
    make_activation_entry,
    make_activation_manifest,
)
from models.projection_manifest import SearchCompat
from models.semantic_digest import SemanticDigestV1
from services.semantic_activation import (
    DIGEST_TIER0_PAYLOAD_SCHEMA_HASH,
    DIGEST_TIER0_PAYLOAD_SCHEMA_V1,
    DigestProjectionCandidate,
    ProjectionActivationRepository,
    SemanticDigestProjectionWorker,
    SemanticActivationError,
    _classify_digest_cache,
    _bind_validated_candidate_to_claim,
    _job_prompt_version_closure,
    _resolve_digest_cache_selection,
    _terminal_quarantine_exclusion,
    _validate_source_rows,
    ensure_activation_contracts,
)
from services.semantic_gateway import (
    SemanticGatewayProvenance,
    semantic_digest_cache_key,
    semantic_digest_prompt_hash,
    semantic_digest_repair_prompt_hash,
    semantic_digest_schema_hash,
)


def _manifest(**changes) -> ProjectionManifestV2:
    values = {
        "family": "document_summary",
        "representation_role": "semantic_digest",
        "source_schema_hashes": {"semantic_digest.v1": semantic_digest_schema_hash()},
        "payload_schema_hash": "sha256:" + "2" * 64,
        "embedding_profile": ActivationEmbeddingProfileV2(
            model_id="Qwen/Qwen3-Embedding-0.6B",
            model_revision="Qwen/Qwen3-Embedding-0.6B",
            dims=1024,
            quantization="binary",
            instruction_version="qwen3-retrieval-query-v1",
            document_side_instruction="raw",
            sparse_recipe_version="none",
        ),
        "search_compat": SearchCompat(
            oversampling=2.0,
            rescore_with_full_vectors=True,
            distance="cosine",
        ),
        "target": ProjectionTargetV2(
            collection_name="polymath_doc_summaries", vector_name="dense"
        ),
        "recipe_version": "semantic_digest_tier0_projection.v1",
        "rollback_predecessor": None,
    }
    values.update(changes)
    return make_activation_manifest(**values)


def _source() -> ProjectionSourceLocatorV2:
    return ProjectionSourceLocatorV2(
        source_kind="semantic_digest_cache",
        source_collection="semantic_digest_cache",
        source_id="sha256:" + "a" * 64,
        ownership_collection="semantic_digest_jobs",
        ownership_id="job:test",
        artifact_id="semantic-digest:test",
        corpus_id="corpus:test",
        doc_id="doc:test",
        source_version_id="source-version:test",
        parent_id="parent:test",
        parent_text_hash="sha256:" + "b" * 64,
        source_child_ids_hash="sha256:" + "c" * 64,
        source_child_count=1,
    )


def test_v2_manifest_identity_includes_exact_target_and_preserves_v1_contract():
    manifest = _manifest()
    changed = _manifest(
        target=ProjectionTargetV2(collection_name="other", vector_name="dense")
    )

    assert manifest.schema_version == "projection_manifest.v2"
    assert manifest.manifest_id != changed.manifest_id
    assert manifest.manifest_id.endswith(
        manifest.projection_profile_hash.split(":", 1)[1]
    )


def test_v2_outbox_identity_is_version_scoped_and_requires_lease_fields():
    manifest = _manifest()
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    entry = make_activation_entry(
        artifact_revision_id="rev:" + "3" * 64,
        manifest_id=manifest.manifest_id,
        point_id="point:test",
        projected_payload_hash="sha256:" + "9" * 64,
        source=_source(),
        now=now,
    )

    assert entry.outbox_id == activation_outbox_id(
        entry.artifact_revision_id, manifest.manifest_id, "upsert"
    )
    with pytest.raises(ValueError, match="active lease"):
        ProjectionOutboxV2(**{**entry.model_dump(), "state": "in_flight"})


def _digest_source_rows():
    digest = SemanticDigestV1(
        schema_version="semantic_digest.v1",
        parent_id="parent:test",
        summary="Feedback changes an operating baseline.",
        central_thesis="Feedback and baselines are causally related.",
        underlying_meanings=[],
        domain_proposals=[],
        frame_proposals=[],
        latent_concepts=[],
        motif_proposals=[],
        conditions=[],
        exceptions=[],
        unresolved_interpretations=[],
    )
    schema_hash = semantic_digest_schema_hash()
    prompt_hash = semantic_digest_prompt_hash(
        "parent-digest.v5", "parent-digest-repair.v2"
    )
    output_hash = namespace_hash("body", digest.model_dump(mode="python"))
    input_hash = "sha256:" + "6" * 64
    model_id = "openai/LongCat-2.0"
    runtime_version = "runtime:v1"
    cache_key = semantic_digest_cache_key(
        input_hash=input_hash,
        model_id=model_id,
        schema_hash=schema_hash,
        prompt_hash=prompt_hash,
        runtime_version=runtime_version,
    )
    provenance = SemanticGatewayProvenance(
        model_id=model_id,
        runtime="provider",
        runtime_version=runtime_version,
        tokenizer_id="provider-managed",
        chat_template_hash="sha256:" + "5" * 64,
        schema_version="semantic_digest.v1",
        schema_hash=schema_hash,
        prompt_version="parent-digest.v5",
        prompt_hash=prompt_hash,
        repair_prompt_version="parent-digest-repair.v2",
        repair_prompt_hash=semantic_digest_repair_prompt_hash(
            "parent-digest-repair.v2"
        ),
        temperature=0,
        input_hash=input_hash,
        output_hash=output_hash,
        capability_tier="tier3",
        capability_detection="test",
        attempts=1,
        repair_attempted=False,
        cache_key=cache_key,
    )
    cache = {
        "_id": cache_key,
        "status": "accepted_cache",
        "serving_eligible": True,
        "canonical_write": False,
        "digest": digest.model_dump(mode="python"),
        "provenance": provenance.model_dump(mode="python"),
    }
    job = {
        "job_id": "job:test",
        "status": "succeeded",
        "corpus_id": "corpus:test",
        "doc_id": "doc:test",
        "parent_id": "parent:test",
        "cache_key": cache_key,
        "input_hash": provenance.input_hash,
        "output_hash": provenance.output_hash,
        "schema_hash": provenance.schema_hash,
        "prompt_hash": provenance.prompt_hash,
        "repair_prompt_hash": provenance.repair_prompt_hash,
        "model_id": provenance.model_id,
        "runtime_version": provenance.runtime_version,
        "prompt_version": provenance.prompt_version,
        "repair_prompt_version": provenance.repair_prompt_version,
    }
    parent_text = "Feedback changes the operating baseline."
    parent = {
        "corpus_id": "corpus:test",
        "doc_id": "doc:test",
        "parent_id": "parent:test",
        "text": parent_text,
        "source_hash": hashlib.sha256(parent_text.encode("utf-8")).hexdigest(),
        "validation_status": "valid",
        "child_ids": ["child:test"],
    }
    children = [
        {
            "corpus_id": "corpus:test",
            "doc_id": "doc:test",
            "parent_id": "parent:test",
            "chunk_id": "child:test",
            "text": parent_text,
        }
    ]
    document = {
        "corpus_id": "corpus:test",
        "doc_id": "doc:test",
        "title": "Feedback",
        "source_type": "markdown",
        "source_identity": {"content_sha256": "7" * 64},
    }
    config = {
        "embedding_model_id": "Qwen/Qwen3-Embedding-0.6B",
        "embedding_dimension": 1024,
        "query_instruction_profile": "baseline_live_v0",
    }
    return cache, job, parent, document, children, config


def test_digest_candidate_closes_provenance_and_never_uses_legacy_point_id():
    candidate = _validate_source_rows(
        cache=_digest_source_rows()[0],
        job=_digest_source_rows()[1],
        parent=_digest_source_rows()[2],
        document=_digest_source_rows()[3],
        children=_digest_source_rows()[4],
        embedding_config=_digest_source_rows()[5],
    )

    assert candidate.text == (
        "Feedback changes an operating baseline.\n"
        "Feedback and baselines are causally related."
    )
    assert candidate.payload["chunk_type"] == "semantic_digest"
    assert candidate.payload["concepts"] == []
    assert candidate.entry.point_id == projection_point_id(
        candidate.entry.source.artifact_id,
        "semantic_digest",
        candidate.manifest.projection_profile_hash,
    )
    legacy_digest = hashlib.md5(b"corpus:test:doc:test:doc_profile").hexdigest()
    legacy_point_id = (
        f"{legacy_digest[:8]}-{legacy_digest[8:12]}-{legacy_digest[12:16]}-"
        f"{legacy_digest[16:20]}-{legacy_digest[20:32]}"
    )
    assert candidate.entry.point_id != legacy_point_id


def test_digest_payload_schema_hash_covers_full_closed_semantic_contract():
    assert DIGEST_TIER0_PAYLOAD_SCHEMA_V1["additionalProperties"] is False
    assert "title" not in DIGEST_TIER0_PAYLOAD_SCHEMA_V1["properties"]
    assert "source_type" not in DIGEST_TIER0_PAYLOAD_SCHEMA_V1["properties"]
    changed = deepcopy(DIGEST_TIER0_PAYLOAD_SCHEMA_V1)
    changed["properties"]["summary"]["description"] += " changed"
    assert namespace_hash("schema", changed) != DIGEST_TIER0_PAYLOAD_SCHEMA_HASH


def test_digest_candidate_fails_closed_on_job_or_source_drift():
    cache, job, parent, document, children, config = _digest_source_rows()
    job["doc_id"] = "doc:other"
    with pytest.raises(SemanticActivationError, match="parent ownership"):
        _validate_source_rows(
            cache=cache,
            job=job,
            parent=parent,
            document=document,
            children=children,
            embedding_config=config,
        )


def test_explicit_faithfulness_quarantine_is_an_accounted_exclusion():
    cache, job, *_ = _digest_source_rows()
    superseded_at = datetime(2026, 7, 15, 11, 15, 30, tzinfo=timezone.utc)
    cache.update(
        {
            "serving_eligible": False,
            "faithfulness_status": "rejected",
            "supersession_reason": "faithfulness_rejected_unsupported_synthesis",
            "superseded_at": superseded_at,
            "superseded_by_cache_key": "sha256:" + "b" * 64,
        }
    )

    exclusion = _classify_digest_cache(cache, job)

    assert exclusion is not None
    assert exclusion.cache_key == cache["_id"]
    assert exclusion.job_id == job["job_id"]
    assert exclusion.parent_id == job["parent_id"]
    assert exclusion.faithfulness_status == "rejected"
    assert exclusion.supersession_reason == (
        "faithfulness_rejected_unsupported_synthesis"
    )
    assert exclusion.superseded_at == superseded_at.isoformat()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda cache: cache.pop("superseded_at"),
        lambda cache: cache.update({"faithfulness_status": "unknown"}),
        lambda cache: cache.update({"supersession_reason": "untyped_reason"}),
        lambda cache: cache.pop("superseded_by_cache_key"),
    ],
)
def test_unexplained_nonserving_digest_fails_closed(mutation):
    cache, job, *_ = _digest_source_rows()
    cache.update(
        {
            "serving_eligible": False,
            "faithfulness_status": "rejected",
            "supersession_reason": "faithfulness_rejected_unsupported_synthesis",
            "superseded_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
            "superseded_by_cache_key": "sha256:" + "b" * 64,
        }
    )
    mutation(cache)

    with pytest.raises(
        SemanticActivationError,
        match="non-serving without typed terminal quarantine",
    ):
        _classify_digest_cache(cache, job)


def _resolved_supersession_rows():
    predecessor_cache, predecessor_job, *_ = _digest_source_rows()
    successor_cache = deepcopy(predecessor_cache)
    successor_job = deepcopy(predecessor_job)
    successor_key = "sha256:" + "b" * 64
    predecessor_cache.update(
        {
            "serving_eligible": False,
            "faithfulness_status": "rejected",
            "supersession_reason": "faithfulness_rejected_unsupported_synthesis",
            "superseded_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
            "superseded_by_cache_key": successor_key,
        }
    )
    successor_cache["_id"] = successor_key
    successor_cache["provenance"]["cache_key"] = successor_key
    successor_job.update({"job_id": "sha256:" + "c" * 64, "cache_key": successor_key})
    return predecessor_cache, predecessor_job, successor_cache, successor_job


def test_explicit_supersession_chain_resolves_to_one_eligible_winner():
    (
        predecessor_cache,
        predecessor_job,
        successor_cache,
        successor_job,
    ) = _resolved_supersession_rows()
    predecessor_key = predecessor_cache["_id"]
    successor_key = successor_cache["_id"]

    eligible, exclusions = _resolve_digest_cache_selection(
        cache_keys=[predecessor_key, successor_key],
        raw_cache_by_key={
            predecessor_key: predecessor_cache,
            successor_key: successor_cache,
        },
        jobs_by_cache={
            predecessor_key: [predecessor_job],
            successor_key: [successor_job],
        },
    )

    assert list(eligible) == [successor_key]
    assert len(exclusions) == 1
    assert exclusions[0].cache_key == predecessor_key
    assert exclusions[0].superseded_by_cache_key == successor_key
    assert exclusions[0].successor_job_id == successor_job["job_id"]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda pc, pj, sc, sj: pc.update(
                {"superseded_by_cache_key": "sha256:" + "f" * 64}
            ),
            "not one serving succeeded cache/job",
        ),
        (
            lambda pc, pj, sc, sj: sj.update({"parent_id": "parent:other"}),
            "ownership drifted",
        ),
        (
            lambda pc, pj, sc, sj: sc.update({"superseded_by_cache_key": pc["_id"]}),
            "itself superseded or quarantined",
        ),
    ],
)
def test_broken_or_cyclic_supersession_chain_fails_closed(mutation, message):
    (
        predecessor_cache,
        predecessor_job,
        successor_cache,
        successor_job,
    ) = _resolved_supersession_rows()
    mutation(predecessor_cache, predecessor_job, successor_cache, successor_job)

    with pytest.raises(SemanticActivationError, match=message):
        _resolve_digest_cache_selection(
            cache_keys=[predecessor_cache["_id"], successor_cache["_id"]],
            raw_cache_by_key={
                predecessor_cache["_id"]: predecessor_cache,
                successor_cache["_id"]: successor_cache,
            },
            jobs_by_cache={
                predecessor_cache["_id"]: [predecessor_job],
                successor_cache["_id"]: [successor_job],
            },
        )


def test_multiple_eligible_successors_for_one_parent_fail_closed():
    (
        predecessor_cache,
        predecessor_job,
        successor_cache,
        successor_job,
    ) = _resolved_supersession_rows()
    second_cache = deepcopy(successor_cache)
    second_job = deepcopy(successor_job)
    second_key = "sha256:" + "d" * 64
    second_cache["_id"] = second_key
    second_job.update({"job_id": "sha256:" + "e" * 64, "cache_key": second_key})

    with pytest.raises(SemanticActivationError, match="multiple serving-eligible"):
        _resolve_digest_cache_selection(
            cache_keys=[
                predecessor_cache["_id"],
                successor_cache["_id"],
                second_key,
            ],
            raw_cache_by_key={
                predecessor_cache["_id"]: predecessor_cache,
                successor_cache["_id"]: successor_cache,
                second_key: second_cache,
            },
            jobs_by_cache={
                predecessor_cache["_id"]: [predecessor_job],
                successor_cache["_id"]: [successor_job],
                second_key: [second_job],
            },
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda rows: rows[1].pop("model_id"), "model_id drifted"),
        (
            lambda rows: rows[1].update({"prompt_hash": "sha256:" + "0" * 64}),
            "prompt_hash drifted",
        ),
        (
            lambda rows: rows[2].update({"source_hash": "0" * 64}),
            "parent source hash drifted",
        ),
        (
            lambda rows: rows[2].update({"child_ids": ["child:other"]}),
            "child set drifted",
        ),
    ],
)
def test_digest_candidate_requires_complete_current_provenance(mutation, message):
    rows = list(_digest_source_rows())
    mutation(rows)
    cache, job, parent, document, children, config = rows
    with pytest.raises(SemanticActivationError, match=message):
        _validate_source_rows(
            cache=cache,
            job=job,
            parent=parent,
            document=document,
            children=children,
            embedding_config=config,
        )


def test_both_legacy_job_prompt_labels_may_close_from_exact_cache_provenance():
    cache, job, parent, document, children, config = _digest_source_rows()
    job.pop("prompt_version")
    job.pop("repair_prompt_version")

    candidate = _validate_source_rows(
        cache=cache,
        job=job,
        parent=parent,
        document=document,
        children=children,
        embedding_config=config,
    )

    closure = candidate.provenance_closure
    assert closure == candidate.payload["provenance_closure"]
    assert closure["mode"] == "legacy_missing_job_prompt_version_labels"
    assert closure["job_id"] == job["job_id"]
    assert closure["cache_key"] == cache["_id"]
    assert closure["adopted_prompt_version"] == "parent-digest.v5"
    assert closure["adopted_repair_prompt_version"] == "parent-digest-repair.v2"


def test_one_sided_legacy_job_prompt_label_omission_fails_closed():
    cache, job, *_ = _digest_source_rows()
    job.pop("prompt_version")
    provenance = SemanticGatewayProvenance.model_validate(cache["provenance"])

    with pytest.raises(SemanticActivationError, match="partially missing"):
        _job_prompt_version_closure(
            cache_key=cache["_id"], job=job, provenance=provenance
        )


def test_legacy_job_prompt_label_closure_requires_cache_labels():
    cache, job, *_ = _digest_source_rows()
    job.pop("prompt_version")
    job.pop("repair_prompt_version")
    cache["provenance"].pop("prompt_version")

    with pytest.raises(ValidationError, match="prompt_version"):
        SemanticGatewayProvenance.model_validate(cache["provenance"])


class _ClaimCollection:
    def __init__(self, row):
        self.row = row
        self.query = None
        self.update = None
        self.update_many_calls = []
        self.find_one_and_update_calls = []

    async def update_many(self, query, update):
        self.update_many_calls.append((query, update))
        return SimpleNamespace(modified_count=0)

    async def find_one_and_update(self, query, update, **kwargs):
        self.find_one_and_update_calls.append((query, update, kwargs))
        expression = query.get("$expr") or {}
        if (
            "$gte" in expression
            and self.row["attempt_count"] < self.row["max_attempts"]
        ):
            return None
        if (
            "$lt" in expression
            and self.row["attempt_count"] >= self.row["max_attempts"]
        ):
            return None
        self.query = query
        self.update = update
        row = dict(self.row)
        row.update(update["$set"])
        if "$inc" in update:
            row["attempt_count"] += update["$inc"]["attempt_count"]
        for field in update.get("$unset", {}):
            row.pop(field, None)
        return row


class _DB:
    def __init__(self, collection):
        self.collection = collection

    def __getitem__(self, name):
        assert name == "projection_outbox"
        return self.collection


class _IndexCollection:
    def __init__(self):
        self.indexes = []

    async def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))


class _IndexDB:
    def __init__(self):
        self.collections = {
            "projection_manifests": _IndexCollection(),
            "projection_outbox": _IndexCollection(),
        }

    def __getitem__(self, name):
        return self.collections[name]


class _ContractDB(_IndexDB):
    def __init__(self):
        super().__init__()
        self.commands = []

    async def command(self, command):
        self.commands.append(command)
        return {"ok": 1}

    async def list_collection_names(self):
        return list(self.collections)


@pytest.mark.asyncio
async def test_activation_preflight_touches_only_projection_contracts():
    db = _ContractDB()

    result = await ensure_activation_contracts(db)

    assert [row["collMod"] for row in db.commands] == [
        "projection_manifests",
        "projection_outbox",
    ]
    assert set(result) == {"projection_manifests", "projection_outbox"}
    assert db.collections["projection_manifests"].indexes
    assert db.collections["projection_outbox"].indexes


@pytest.mark.asyncio
async def test_v2_unique_indexes_are_partial_and_leave_v1_rows_untouched():
    db = _IndexDB()
    await ProjectionActivationRepository(db).ensure_indexes()

    manifest_unique = db.collections["projection_manifests"].indexes[0][1]
    outbox_unique = db.collections["projection_outbox"].indexes[0][1]
    assert manifest_unique["unique"] is True
    assert manifest_unique["partialFilterExpression"] == {
        "schema_version": "projection_manifest.v2"
    }
    assert outbox_unique["unique"] is True
    assert outbox_unique["partialFilterExpression"] == {
        "schema_version": "projection_outbox.v2"
    }


class _InsertCollection:
    def __init__(self, row=None):
        self.row = deepcopy(row)
        self.updates = []

    async def update_one(self, query, update, **kwargs):
        self.updates.append((query, update, kwargs))
        if self.row is None:
            self.row = deepcopy(update["$setOnInsert"])
        return SimpleNamespace(modified_count=0)

    async def find_one(self, query, projection=None):
        return deepcopy(self.row)


@pytest.mark.asyncio
async def test_enqueue_rerun_is_noop_and_preserves_original_created_at():
    manifest = _manifest()
    first_at = datetime(2026, 7, 16, 10, tzinfo=timezone.utc)
    rerun_at = first_at + timedelta(hours=2)
    first = make_activation_entry(
        artifact_revision_id="rev:" + "8" * 64,
        manifest_id=manifest.manifest_id,
        point_id="point:test",
        projected_payload_hash="sha256:" + "9" * 64,
        source=_source(),
        now=first_at,
    )
    rerun = make_activation_entry(
        artifact_revision_id=first.artifact_revision_id,
        manifest_id=first.manifest_id,
        point_id=first.point_id,
        projected_payload_hash=first.projected_payload_hash,
        source=first.source,
        now=rerun_at,
    )
    collection = _InsertCollection(first.model_dump(mode="python"))
    repository = ProjectionActivationRepository(_DB(collection))

    await repository.enqueue(rerun)

    assert collection.row["created_at"] == first_at
    assert collection.updates[0][1] == {"$setOnInsert": rerun.model_dump(mode="python")}
    with pytest.raises(SemanticActivationError, match="immutable.*collision"):
        await repository.enqueue(rerun.model_copy(update={"point_id": "point:other"}))


@pytest.mark.asyncio
async def test_repository_reclaims_expired_lease_with_atomic_attempt_increment():
    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    manifest = _manifest()
    pending = make_activation_entry(
        artifact_revision_id="rev:" + "8" * 64,
        manifest_id=manifest.manifest_id,
        point_id="point:test",
        projected_payload_hash="sha256:" + "9" * 64,
        source=_source(),
        now=now - timedelta(hours=1),
    )
    expired = pending.model_copy(
        update={
            "state": "in_flight",
            "attempt_count": 1,
            "updated_at": now - timedelta(minutes=10),
            "lease_owner": "dead-worker",
            "lease_expires_at": now - timedelta(minutes=5),
        }
    )
    collection = _ClaimCollection(expired.model_dump(mode="python"))
    claimed = await ProjectionActivationRepository(_DB(collection)).claim_one(
        owner="new-worker", now=now, corpus_ids=["corpus:test"]
    )

    assert claimed is not None
    assert claimed.state == "in_flight"
    assert claimed.attempt_count == 2
    assert claimed.lease_owner == "new-worker"
    assert {
        "state": "in_flight",
        "lease_expires_at": {"$lte": now},
    } in collection.query["$or"]
    assert collection.query["source.corpus_id"] == {"$in": ["corpus:test"]}


@pytest.mark.asyncio
async def test_repository_reclaims_expired_exhausted_lease_for_reconciliation():
    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    manifest = _manifest()
    pending = make_activation_entry(
        artifact_revision_id="rev:" + "8" * 64,
        manifest_id=manifest.manifest_id,
        point_id="point:test",
        projected_payload_hash="sha256:" + "9" * 64,
        source=_source(),
        now=now - timedelta(hours=1),
        max_attempts=2,
    )
    expired = pending.model_copy(
        update={
            "state": "in_flight",
            "attempt_count": 2,
            "updated_at": now - timedelta(minutes=10),
            "lease_owner": "dead-worker",
            "lease_expires_at": now - timedelta(minutes=5),
        }
    )
    collection = _ClaimCollection(expired.model_dump(mode="python"))

    claimed = await ProjectionActivationRepository(
        _DB(collection)
    ).claim_reconciliation_one(owner="new-worker", now=now)

    assert claimed is not None
    assert claimed.state == "in_flight"
    assert claimed.attempt_count == claimed.max_attempts == 2
    assert claimed.lease_owner == "new-worker"
    query, update, _kwargs = collection.find_one_and_update_calls[0]
    assert query["$expr"] == {"$gte": ["$attempt_count", "$max_attempts"]}
    assert update["$set"]["lease_owner"] == "new-worker"
    assert "$inc" not in update


class _WorkerRepository:
    def __init__(
        self,
        entries,
        *,
        reconciliation_entries=(),
        fail_apply_point=None,
        manifest=None,
    ):
        self.entries = list(entries)
        self.reconciliation_entries = list(reconciliation_entries)
        self.fail_apply_point = fail_apply_point
        self.manifest = manifest
        self.claim_calls = 0
        self.renewed = []
        self.renewed_attempts = []
        self.applied = []
        self.applied_attempts = []
        self.failed = []
        self.failed_attempts = []

    async def ensure_indexes(self):
        return None

    async def claim_one(self, **kwargs):
        self.claim_calls += 1
        return self.entries.pop(0) if self.entries else None

    async def claim_reconciliation_one(self, **kwargs):
        return (
            self.reconciliation_entries.pop(0) if self.reconciliation_entries else None
        )

    async def renew_lease(self, entry, **kwargs):
        self.renewed.append(entry.point_id)
        self.renewed_attempts.append(entry.attempt_count)

    async def load_manifest(self, manifest_id):
        assert self.manifest is not None
        assert self.manifest.manifest_id == manifest_id
        return self.manifest

    async def mark_applied(self, entry, **kwargs):
        if entry.point_id == self.fail_apply_point:
            raise RuntimeError("simulated acknowledgement outage")
        self.applied.append(entry.point_id)
        self.applied_attempts.append(entry.attempt_count)

    async def mark_failed(self, entry, **kwargs):
        self.failed.append(entry.point_id)
        self.failed_attempts.append(entry.attempt_count)


class _QdrantReceiptClient:
    def __init__(self):
        self.points = {}

    async def upsert(self, *, points, **kwargs):
        for point in points:
            self.points[str(point.id)] = dict(point.payload or {})
        return SimpleNamespace(operation_id=17)

    async def retrieve(self, *, ids, **kwargs):
        return [
            SimpleNamespace(id=point_id, payload=deepcopy(self.points[str(point_id)]))
            for point_id in ids
            if str(point_id) in self.points
        ]


def test_validated_candidate_rebinds_durable_attempt_and_rejects_identity_drift():
    cache, job, parent, document, children, config = _digest_source_rows()
    candidate = _validate_source_rows(
        cache=cache,
        job=job,
        parent=parent,
        document=document,
        children=children,
        embedding_config=config,
    )
    now = datetime.now(timezone.utc)
    claimed = candidate.entry.model_copy(
        update={
            "state": "in_flight",
            "attempt_count": 2,
            "lease_owner": "worker:test",
            "lease_expires_at": now + timedelta(minutes=10),
        }
    )

    rebound = _bind_validated_candidate_to_claim(candidate, claimed)

    assert rebound.entry == claimed
    assert rebound.entry.attempt_count == 2
    assert rebound.entry.lease_owner == "worker:test"
    with pytest.raises(SemanticActivationError, match="point identity drifted"):
        _bind_validated_candidate_to_claim(
            candidate,
            claimed.model_copy(
                update={"point_id": "11111111-1111-1111-1111-111111111111"}
            ),
        )


@pytest.mark.asyncio
async def test_drain_renew_apply_and_failure_use_durable_claim_attempt(monkeypatch):
    cache, job, parent, document, children, config = _digest_source_rows()
    candidate = _validate_source_rows(
        cache=cache,
        job=job,
        parent=parent,
        document=document,
        children=children,
        embedding_config=config,
    )
    now = datetime.now(timezone.utc)
    claimed = candidate.entry.model_copy(
        update={
            "state": "in_flight",
            "attempt_count": 2,
            "lease_owner": "worker:test",
            "lease_expires_at": now + timedelta(minutes=10),
        }
    )
    rebound = _bind_validated_candidate_to_claim(candidate, claimed)
    repository = _WorkerRepository([claimed])
    worker = SemanticDigestProjectionWorker(
        object(), _QdrantReceiptClient(), owner="worker:test"
    )
    worker.repository = repository
    worker._candidate_for_entry = AsyncMock(return_value=rebound)
    monkeypatch.setattr(
        "services.embedder.embed_documents",
        AsyncMock(return_value=[[0.1] * 1024]),
    )
    monkeypatch.setattr(
        "services.ingestion.tier0._ensure_collection", AsyncMock(return_value=None)
    )

    counts = await worker.drain_batch()

    assert counts["applied"] == 1
    assert repository.renewed_attempts == [2]
    assert repository.applied_attempts == [2]

    failed_repository = _WorkerRepository([claimed])
    failed_worker = SemanticDigestProjectionWorker(
        object(), _QdrantReceiptClient(), owner="worker:test"
    )
    failed_worker.repository = failed_repository
    failed_worker._candidate_for_entry = AsyncMock(return_value=rebound)
    monkeypatch.setattr(
        "services.embedder.embed_documents",
        AsyncMock(side_effect=RuntimeError("embedding unavailable")),
    )

    failed_counts = await failed_worker.drain_batch()

    assert failed_counts["failed"] == 1
    assert failed_repository.renewed_attempts == [2]
    assert failed_repository.failed_attempts == [2]


@pytest.mark.asyncio
async def test_drain_partial_ack_never_regresses_applied_qdrant_rows(monkeypatch):
    cache, job, parent, document, children, config = _digest_source_rows()
    base = _validate_source_rows(
        cache=cache,
        job=job,
        parent=parent,
        document=document,
        children=children,
        embedding_config=config,
    )
    now = datetime.now(timezone.utc)
    first_entry = base.entry.model_copy(
        update={
            "state": "in_flight",
            "attempt_count": 1,
            "lease_owner": "worker:test",
            "lease_expires_at": now + timedelta(minutes=10),
        }
    )
    second_entry = first_entry.model_copy(
        update={"point_id": "11111111-1111-1111-1111-111111111111"}
    )
    candidates = [
        replace(base, entry=first_entry),
        replace(base, entry=second_entry),
    ]
    repository = _WorkerRepository(
        [first_entry, second_entry], fail_apply_point=second_entry.point_id
    )
    worker = SemanticDigestProjectionWorker(
        object(), _QdrantReceiptClient(), owner="worker:test"
    )
    worker.repository = repository
    worker._candidate_for_entry = AsyncMock(side_effect=candidates)
    monkeypatch.setattr(
        "services.embedder.embed_documents",
        AsyncMock(return_value=[[0.1] * 1024, [0.2] * 1024]),
    )
    monkeypatch.setattr(
        "services.ingestion.tier0._ensure_collection", AsyncMock(return_value=None)
    )

    counts = await worker.drain_batch(limit=128)

    assert counts == {
        "claimed": 2,
        "applied": 1,
        "reconciled": 0,
        "failed": 0,
        "dead": 0,
        "ack_pending": 1,
    }
    assert repository.renewed == [first_entry.point_id, second_entry.point_id]
    assert repository.applied == [first_entry.point_id]
    assert repository.failed == []


@pytest.mark.asyncio
async def test_drain_batch_caps_claim_exposure_at_32(monkeypatch):
    now = datetime.now(timezone.utc)
    manifest = _manifest()
    entry = make_activation_entry(
        artifact_revision_id="rev:" + "8" * 64,
        manifest_id=manifest.manifest_id,
        point_id="point:test",
        projected_payload_hash="sha256:" + "9" * 64,
        source=_source(),
        now=now,
    ).model_copy(
        update={
            "state": "in_flight",
            "attempt_count": 1,
            "lease_owner": "worker:test",
            "lease_expires_at": now + timedelta(minutes=10),
        }
    )
    repository = _WorkerRepository([entry] * 40)
    worker = SemanticDigestProjectionWorker(object(), object(), owner="worker:test")
    worker.repository = repository
    worker._candidate_for_entry = AsyncMock(side_effect=RuntimeError("reject"))

    counts = await worker.drain_batch(limit=128)

    assert counts["claimed"] == 32
    assert counts["failed"] == 32
    assert repository.claim_calls == 32


@pytest.mark.asyncio
async def test_exhausted_row_reconciles_existing_exact_qdrant_payload():
    cache, job, parent, document, children, config = _digest_source_rows()
    candidate = _validate_source_rows(
        cache=cache,
        job=job,
        parent=parent,
        document=document,
        children=children,
        embedding_config=config,
    )
    now = datetime.now(timezone.utc)
    exhausted = candidate.entry.model_copy(
        update={
            "state": "in_flight",
            "attempt_count": candidate.entry.max_attempts,
            "lease_owner": "worker:test",
            "lease_expires_at": now + timedelta(minutes=10),
        }
    )
    qdrant = _QdrantReceiptClient()
    qdrant.points[exhausted.point_id] = deepcopy(candidate.payload)
    repository = _WorkerRepository(
        [], reconciliation_entries=[exhausted], manifest=candidate.manifest
    )
    worker = SemanticDigestProjectionWorker(object(), qdrant, owner="worker:test")
    worker.repository = repository
    worker._candidate_for_entry = AsyncMock(
        side_effect=AssertionError("reconciliation must precede source rebuild")
    )

    counts = await worker.drain_batch()

    assert counts["applied"] == 1
    assert counts["reconciled"] == 1
    assert counts["dead"] == 0
    assert repository.applied == [exhausted.point_id]
    assert repository.failed == []
    worker._candidate_for_entry.assert_not_awaited()


@pytest.mark.asyncio
async def test_exhausted_row_dead_letters_only_after_absence_is_reconciled():
    cache, job, parent, document, children, config = _digest_source_rows()
    candidate = _validate_source_rows(
        cache=cache,
        job=job,
        parent=parent,
        document=document,
        children=children,
        embedding_config=config,
    )
    now = datetime.now(timezone.utc)
    exhausted = candidate.entry.model_copy(
        update={
            "state": "in_flight",
            "attempt_count": candidate.entry.max_attempts,
            "lease_owner": "worker:test",
            "lease_expires_at": now + timedelta(minutes=10),
        }
    )
    repository = _WorkerRepository(
        [], reconciliation_entries=[exhausted], manifest=candidate.manifest
    )
    worker = SemanticDigestProjectionWorker(
        object(), _QdrantReceiptClient(), owner="worker:test"
    )
    worker.repository = repository

    counts = await worker.drain_batch()

    assert counts["applied"] == 0
    assert counts["reconciled"] == 0
    assert counts["dead"] == 1
    assert repository.failed == [exhausted.point_id]


class _CommitThenRaiseQdrant(_QdrantReceiptClient):
    async def upsert(self, *, points, **kwargs):
        await super().upsert(points=points, **kwargs)
        raise RuntimeError("transport failed after remote commit")


class _UnavailableReconcileQdrant(_QdrantReceiptClient):
    async def upsert(self, *, points, **kwargs):
        raise RuntimeError("write outcome unknown")

    async def retrieve(self, *, ids, **kwargs):
        raise RuntimeError("qdrant unavailable during reconciliation")


def _terminal_attempt_candidate():
    cache, job, parent, document, children, config = _digest_source_rows()
    base = _validate_source_rows(
        cache=cache,
        job=job,
        parent=parent,
        document=document,
        children=children,
        embedding_config=config,
    )
    now = datetime.now(timezone.utc)
    entry = base.entry.model_copy(
        update={
            "state": "in_flight",
            "attempt_count": base.entry.max_attempts,
            "lease_owner": "worker:test",
            "lease_expires_at": now + timedelta(minutes=10),
        }
    )
    return replace(base, entry=entry)


@pytest.mark.asyncio
async def test_final_attempt_commit_then_raise_reconciles_to_applied(monkeypatch):
    candidate = _terminal_attempt_candidate()
    repository = _WorkerRepository([candidate.entry], manifest=candidate.manifest)
    worker = SemanticDigestProjectionWorker(
        object(), _CommitThenRaiseQdrant(), owner="worker:test"
    )
    worker.repository = repository
    worker._candidate_for_entry = AsyncMock(return_value=candidate)
    monkeypatch.setattr(
        "services.embedder.embed_documents",
        AsyncMock(return_value=[[0.1] * 1024]),
    )
    monkeypatch.setattr(
        "services.ingestion.tier0._ensure_collection", AsyncMock(return_value=None)
    )

    counts = await worker.drain_batch()

    assert counts["applied"] == 1
    assert counts["reconciled"] == 1
    assert counts["dead"] == 0
    assert counts["ack_pending"] == 0
    assert repository.applied == [candidate.entry.point_id]
    assert repository.failed == []


@pytest.mark.asyncio
async def test_final_attempt_unknown_write_and_unavailable_reconcile_stays_reclaimable(
    monkeypatch,
):
    candidate = _terminal_attempt_candidate()
    repository = _WorkerRepository([candidate.entry], manifest=candidate.manifest)
    worker = SemanticDigestProjectionWorker(
        object(), _UnavailableReconcileQdrant(), owner="worker:test"
    )
    worker.repository = repository
    worker._candidate_for_entry = AsyncMock(return_value=candidate)
    monkeypatch.setattr(
        "services.embedder.embed_documents",
        AsyncMock(return_value=[[0.1] * 1024]),
    )
    monkeypatch.setattr(
        "services.ingestion.tier0._ensure_collection", AsyncMock(return_value=None)
    )

    counts = await worker.drain_batch()

    assert counts["applied"] == 0
    assert counts["dead"] == 0
    assert counts["ack_pending"] == 1
    assert repository.applied == []
    assert repository.failed == []

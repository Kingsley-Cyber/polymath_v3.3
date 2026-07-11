import pytest

from services.ingestion.document_pipeline_jobs import (
    build_document_pipeline_job,
    classify_document_pipeline_jobs,
    document_pipeline_contract_hash,
    document_pipeline_job_id,
    plan_document_pipeline_jobs,
    reconcile_satisfied_document_pipeline_jobs,
    run_document_pipeline_jobs,
)


@pytest.mark.asyncio
async def test_artifact_reconciliation_retires_failed_chunk_job_when_chunks_exist():
    db = _Db(
        docs=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "write_state": {"mongo_written": True},
            }
        ],
        child_count=3,
    )
    db["document_pipeline_jobs"].rows = [
        {
            "job_id": "poison-job",
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "kind": "chunk_document",
            "status": "failed",
            "attempt_count": 170,
        }
    ]

    await reconcile_satisfied_document_pipeline_jobs(db, corpus_id="corpus-1")

    update = db["document_pipeline_jobs"].bulk_ops[0]._doc["$set"]
    assert update["status"] == "superseded"
    assert update["reason"] == "artifact_already_satisfied"
from services.ingestion.document_pipeline_executors import (
    _stage_after_qdrant,
    mark_documents_persisted_from_artifacts,
)


def test_document_pipeline_job_id_is_deterministic():
    first = document_pipeline_job_id(
        corpus_id="c",
        doc_id="d",
        kind="embed_document",
        source_fingerprint="source",
        contract_hash="contract",
    )
    second = document_pipeline_job_id(
        corpus_id="c",
        doc_id="d",
        kind="embed_document",
        source_fingerprint="source",
        contract_hash="contract",
    )

    assert first == second
    assert first.startswith("doc_stage_")


def test_document_pipeline_contract_hash_tracks_embedding_and_chunking():
    base = {
        "embedding_model_id": "embed-a",
        "ingestion_config": {
            "embedding_model_id": "embed-a",
            "child_chunk_algorithm": "semantic_split",
        },
    }
    changed = {
        "embedding_model_id": "embed-b",
        "ingestion_config": {
            "embedding_model_id": "embed-b",
            "child_chunk_algorithm": "semantic_split",
        },
    }

    assert document_pipeline_contract_hash(base) != document_pipeline_contract_hash(changed)


def test_document_pipeline_job_carries_stage_identity():
    job = build_document_pipeline_job(
        doc={
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "filename": "book.pdf",
            "source_identity": {"content_sha256": "source-hash", "source_key": "sha256:source-hash"},
            "embedding_model_id": "embed-a",
            "ingestion_config": {"embedding_dimension": 1024, "embed_mode": "local"},
            "write_state": {"mongo_written": True},
        },
        kind="embed_document",
        child_chunks=3,
        parent_chunks=1,
    )

    assert job["stage_identity"]["identity_version"] == "stage_identity.v1"
    assert job["stage_identity"]["source_file_hash"] == "source-hash"
    assert job["stage_identity"]["source_key"] == "sha256:source-hash"
    assert job["stage_identity"]["embedding_model_hash"]
    assert job["stage_identity"]["pipeline_contract_hash"] == job["pipeline_contract_hash"]


def test_classify_missing_chunks_as_chunk_job():
    jobs = classify_document_pipeline_jobs(
        doc={
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "filename": "book.pdf",
            "write_state": {},
        },
        child_chunks=0,
        parent_chunks=0,
    )

    assert [job["kind"] for job in jobs] == ["chunk_document"]
    assert jobs[0]["status"] == "queued"
    assert jobs[0]["reason"] == "missing_chunks"


def test_chunk_job_blocks_without_source_pointer():
    job = build_document_pipeline_job(
        doc={"corpus_id": "corpus-1", "doc_id": "doc-1", "write_state": {}},
        kind="chunk_document",
        child_chunks=0,
        parent_chunks=0,
    )

    assert job["status"] == "blocked_no_source"
    assert job["reason"] == "source_pointer_missing"


def test_skipped_duplicate_is_terminal_document_pipeline_work():
    jobs = classify_document_pipeline_jobs(
        doc={
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "filename": "duplicate.pdf",
            "ingest_stage": "skipped_duplicate",
            "write_state": {},
        },
        child_chunks=0,
        parent_chunks=0,
    )

    assert len(jobs) == 1
    assert jobs[0]["kind"] == "chunk_document"
    assert jobs[0]["status"] == "skipped"
    assert jobs[0]["reason"] == "duplicate_document"


def test_classify_existing_chunks_missing_mongo_and_qdrant():
    jobs = classify_document_pipeline_jobs(
        doc={
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "filename": "book.pdf",
            "write_state": {"mongo_written": False, "qdrant_written": False},
        },
        child_chunks=8,
        parent_chunks=2,
    )

    assert [job["kind"] for job in jobs] == ["persist_document", "embed_document"]
    assert jobs[0]["status"] == "queued"
    assert jobs[1]["status"] == "blocked_mongo_state"


def test_classify_existing_chunks_missing_vectors_as_embed_job():
    jobs = classify_document_pipeline_jobs(
        doc={
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "filename": "book.pdf",
            "write_state": {"mongo_written": True, "qdrant_written": False},
        },
        child_chunks=8,
        parent_chunks=2,
    )

    assert [job["kind"] for job in jobs] == ["embed_document"]
    assert jobs[0]["status"] == "queued"
    assert jobs[0]["reason"] == "missing_qdrant_vectors"


def test_classify_verified_qdrant_count_mismatch_as_embed_job():
    jobs = classify_document_pipeline_jobs(
        doc={
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "filename": "book.pdf",
            "write_state": {
                "mongo_written": True,
                "qdrant_written": True,
                "verified": False,
                "verify_errors": [
                    "mismatch: expected=240 child vectors but corpus_x_naive has 220 child vectors"
                ],
            },
        },
        child_chunks=240,
        parent_chunks=20,
    )

    assert [job["kind"] for job in jobs] == ["embed_document"]
    assert jobs[0]["status"] == "queued"
    assert jobs[0]["reason"] == "qdrant_vector_mismatch"


def test_qdrant_executor_stage_does_not_overstate_full_enrichment():
    class _Config:
        use_neo4j = False

    stage, enrichment_status, reason = _stage_after_qdrant(
        config=_Config(),
        doc={"write_state": {"qdrant_written": True, "verified": False}},
        summary_gate_required=False,
        summaries_indexed=False,
    )

    assert stage == "queryable"
    assert enrichment_status == {"summary": "complete", "graph": "complete"}
    assert reason is None


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self._limit = len(rows)

    def limit(self, value):
        self._limit = value
        return self

    def sort(self, *_args, **_kwargs):
        return self

    async def to_list(self, length=None):
        limit = self._limit if length is None else min(self._limit, length)
        return self.rows[:limit]


class _Collection:
    def __init__(self, rows=None, *, count=0):
        self.rows = rows or []
        self.count = count
        self.bulk_ops = []

    async def find_one(self, query=None, *_args, **_kwargs):
        query = query or {}
        for row in self.rows:
            matched = True
            for key, value in query.items():
                if key.startswith("$"):
                    continue
                current = row
                for part in key.split("."):
                    current = current.get(part) if isinstance(current, dict) else None
                if isinstance(value, dict):
                    if "$in" in value and current not in value["$in"]:
                        matched = False
                        break
                    if "$ne" in value and current == value["$ne"]:
                        matched = False
                        break
                elif current != value:
                    matched = False
                    break
            if matched:
                return row
        return None

    def find(self, *_args, **_kwargs):
        return _Cursor(self.rows)

    async def count_documents(self, _query):
        return self.count

    async def bulk_write(self, ops, **_kwargs):
        self.bulk_ops.extend(ops)
        return None

    async def update_one(self, query, update, **_kwargs):
        row = await self.find_one(query)
        if row is None:
            return type("Result", (), {"modified_count": 0})()
        for key, value in (update.get("$set") or {}).items():
            target = row
            parts = key.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
        for key, value in (update.get("$inc") or {}).items():
            row[key] = int(row.get(key) or 0) + int(value)
        return type("Result", (), {"modified_count": 1})()

    def aggregate(self, *_args, **_kwargs):
        return _Cursor([])


class _Db:
    def __init__(self, *, docs, child_count=0, parent_count=0):
        self.collections = {
            "documents": _Collection(docs),
            "chunks": _Collection(count=child_count),
            "parent_chunks": _Collection(count=parent_count),
            "document_pipeline_jobs": _Collection(),
        }

    def __getitem__(self, name):
        return self.collections[name]


@pytest.mark.asyncio
async def test_plan_document_pipeline_jobs_materializes_embed_gap():
    db = _Db(
        docs=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "user_id": "user-1",
                "filename": "book.pdf",
                "write_state": {"mongo_written": True, "qdrant_written": False},
            }
        ],
        child_count=4,
        parent_count=1,
    )

    result = await plan_document_pipeline_jobs(
        db,
        corpus_id="corpus-1",
        apply=True,
        limit=10,
    )

    assert result["status"] == "complete"
    assert result["planned"] == 1
    assert result["counts"] == {"queued": 1}
    assert result["kind_counts"] == {"embed_document": 1}
    assert [type(op).__name__ for op in db["document_pipeline_jobs"].bulk_ops].count("UpdateOne") == 1
    assert [type(op).__name__ for op in db["document_pipeline_jobs"].bulk_ops].count("UpdateMany") == 1
    assert result["superseded"] == 0


@pytest.mark.asyncio
async def test_run_document_pipeline_jobs_reconciles_embed_success_from_write_state():
    doc = {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "user_id": "user-1",
        "filename": "book.pdf",
        "write_state": {"mongo_written": True, "qdrant_written": True},
    }
    db = _Db(docs=[doc], child_count=4, parent_count=1)
    job = build_document_pipeline_job(
        doc=doc,
        kind="embed_document",
        child_chunks=4,
        parent_chunks=1,
    )
    db["document_pipeline_jobs"].rows = [{**job, "updated_at": 1, "attempt_count": 0}]

    result = await run_document_pipeline_jobs(db, corpus_id="corpus-1", user_id="user-1")

    assert result["status"] == "complete"
    assert result["claimed"] == 1
    assert result["counts"] == {"succeeded": 1}
    assert result["jobs"][0]["reason"] == "qdrant_write_complete"


@pytest.mark.asyncio
async def test_run_document_pipeline_jobs_keeps_embed_blocked_until_mongo_written():
    doc = {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "user_id": "user-1",
        "filename": "book.pdf",
        "write_state": {"mongo_written": False, "qdrant_written": False},
    }
    db = _Db(docs=[doc], child_count=4, parent_count=1)
    job = build_document_pipeline_job(
        doc=doc,
        kind="embed_document",
        child_chunks=4,
        parent_chunks=1,
    )
    db["document_pipeline_jobs"].rows = [{**job, "status": "queued", "updated_at": 1}]

    result = await run_document_pipeline_jobs(db, corpus_id="corpus-1", user_id="user-1")

    assert result["status"] == "blocked"
    assert result["counts"] == {"blocked_mongo_state": 1}
    assert result["jobs"][0]["reason"] == "mongo_write_not_complete"


@pytest.mark.asyncio
async def test_run_document_pipeline_jobs_reports_missing_embed_executor():
    doc = {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "user_id": "user-1",
        "filename": "book.pdf",
        "write_state": {"mongo_written": True, "qdrant_written": False},
    }
    db = _Db(docs=[doc], child_count=4, parent_count=1)
    job = build_document_pipeline_job(
        doc=doc,
        kind="embed_document",
        child_chunks=4,
        parent_chunks=1,
    )
    db["document_pipeline_jobs"].rows = [{**job, "status": "queued", "updated_at": 1}]

    result = await run_document_pipeline_jobs(db, corpus_id="corpus-1", user_id="user-1")

    assert result["status"] == "executor_unavailable"
    assert result["executor_missing_kinds"] == ["embed_document"]
    assert result["counts"] == {"queued": 1}
    assert result["jobs"][0]["reason"] == "missing_qdrant_vectors"


@pytest.mark.asyncio
async def test_run_document_pipeline_jobs_executes_embed_runner_and_reconciles_success():
    doc = {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "user_id": "user-1",
        "filename": "book.pdf",
        "write_state": {"mongo_written": True, "qdrant_written": False},
    }
    db = _Db(docs=[doc], child_count=4, parent_count=1)
    job = build_document_pipeline_job(
        doc=doc,
        kind="embed_document",
        child_chunks=4,
        parent_chunks=1,
    )
    db["document_pipeline_jobs"].rows = [{**job, "status": "queued", "updated_at": 1}]

    async def embed_runner(*, doc_ids, limit):
        assert doc_ids == ["doc-1"]
        assert limit == 1
        doc["write_state"]["qdrant_written"] = True
        return {"status": "complete", "counts": {"succeeded": 1}}

    result = await run_document_pipeline_jobs(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        embed_runner=embed_runner,
    )

    assert result["status"] == "complete"
    assert result["runner_results"]["embed_document"]["status"] == "complete"
    assert result["executor_missing_kinds"] == []
    assert result["counts"] == {"succeeded": 1}
    assert result["jobs"][0]["reason"] == "qdrant_write_complete"


@pytest.mark.asyncio
async def test_run_document_pipeline_jobs_marks_executor_reported_failure():
    doc = {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "user_id": "user-1",
        "filename": "book.pdf",
        "write_state": {"mongo_written": True, "qdrant_written": False},
    }
    db = _Db(docs=[doc], child_count=4, parent_count=1)
    job = build_document_pipeline_job(
        doc=doc,
        kind="embed_document",
        child_chunks=4,
        parent_chunks=1,
    )
    db["document_pipeline_jobs"].rows = [{**job, "status": "queued", "updated_at": 1}]

    async def embed_runner(*, doc_ids, limit):
        return {"status": "partial", "counts": {"failed": 1}, "reason": "qdrant down"}

    result = await run_document_pipeline_jobs(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        embed_runner=embed_runner,
    )

    assert result["status"] == "partial"
    assert result["counts"] == {"failed": 1}
    assert result["jobs"][0]["reason"] == "executor_error"


@pytest.mark.asyncio
async def test_mark_documents_persisted_from_artifacts_sets_mongo_write_state():
    doc = {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "user_id": "user-1",
        "filename": "book.pdf",
        "write_state": {"mongo_written": False, "qdrant_written": False},
    }
    db = _Db(docs=[doc], child_count=1, parent_count=1)
    db["chunks"].rows = [
        {
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "chunk_id": "chunk-1",
            "text": "Text",
        }
    ]
    db["parent_chunks"].rows = [
        {
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "parent_id": "parent-1",
            "text": "Text",
            "summary": "Summary",
        }
    ]

    result = await mark_documents_persisted_from_artifacts(
        db,
        corpus_id="corpus-1",
        doc_ids=["doc-1"],
        limit=1,
    )

    assert result["status"] == "complete"
    assert result["counts"] == {"succeeded": 1}
    assert doc["write_state"]["mongo_written"] is True
    assert doc["child_count"] == 1
    assert doc["parent_count"] == 1
    assert doc["summary_count"] == 1


@pytest.mark.asyncio
async def test_run_document_pipeline_jobs_requests_source_runner_for_chunk_jobs():
    doc = {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "user_id": "user-1",
        "filename": "book.pdf",
        "source_path": "/ingest-source/book.pdf",
        "write_state": {},
    }
    db = _Db(docs=[doc], child_count=0, parent_count=0)
    job = build_document_pipeline_job(
        doc=doc,
        kind="chunk_document",
        child_chunks=0,
        parent_chunks=0,
    )
    db["document_pipeline_jobs"].rows = [{**job, "updated_at": 1, "attempt_count": 0}]

    async def source_runner(*, limit):
        assert limit == 1
        return {"status": "started", "eligible_items": 1, "runners_started": 1}

    result = await run_document_pipeline_jobs(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        source_runner=source_runner,
    )

    assert result["status"] == "requested"
    assert result["source_requested"] is True
    assert result["counts"] == {"queued": 1}
    assert result["jobs"][0]["reason"] == "source_parse_requested"

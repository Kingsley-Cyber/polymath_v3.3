import pytest

from services.ingestion.summary_jobs import (
    backfill_summary_stage_identity,
    build_document_summary_job,
    build_parent_summary_job,
    classify_document_summary_status,
    plan_summary_jobs,
    run_summary_jobs,
    summary_contract_hash,
    summary_job_id,
    summary_provider_contract,
)


def test_summary_job_id_is_deterministic_and_kind_scoped():
    parent_id = summary_job_id(
        corpus_id="c",
        kind="retrieval_parent_summary",
        target_id="p",
        source_hash="h",
        contract_hash="contract",
    )
    doc_id = summary_job_id(
        corpus_id="c",
        kind="document_summary",
        target_id="p",
        source_hash="h",
        contract_hash="contract",
    )

    assert parent_id.startswith("summary_parent_")
    assert doc_id.startswith("summary_doc_")
    assert parent_id != doc_id


def test_summary_provider_contract_masks_keys_and_hash_tracks_pool():
    corpus = {
        "default_ingestion_config": {
            "summary_models": [
                {
                    "provider_preset": "deepseek",
                    "model": "deepseek/deepseek-v4-flash",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "never-persist",
                    "max_concurrent": 45,
                }
            ],
            "max_summary_tokens": 420,
        }
    }
    changed = {
        "default_ingestion_config": {
            **corpus["default_ingestion_config"],
            "summary_models": [
                {
                    "provider_preset": "siliconflow",
                    "model": "tencent/Hy3",
                    "base_url": "https://api.siliconflow.cn/v1",
                    "api_key": "also-secret",
                    "max_concurrent": 8,
                }
            ],
        }
    }

    contract = summary_provider_contract(corpus)

    assert contract["pool_source"] == "corpus_summary_models"
    assert contract["pool_size"] == 1
    assert contract["lanes"][0]["model"] == "deepseek/deepseek-v4-flash"
    assert "never-persist" not in str(contract)
    assert summary_contract_hash(corpus) != summary_contract_hash(changed)


def test_parent_summary_job_blocks_empty_parent_text():
    job = build_parent_summary_job(
        parent={
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "parent_id": "parent-1",
            "text": "",
        },
        doc={"user_id": "user-1", "filename": "book.md"},
        corpus={"default_ingestion_config": {}},
    )

    assert job["kind"] == "retrieval_parent_summary"
    assert job["status"] == "blocked_empty_source"
    assert job["reason"] == "empty_parent_text"


def test_summary_jobs_carry_stage_identity():
    doc = {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "user_id": "user-1",
        "filename": "book.md",
        "source_identity": {"content_sha256": "source-hash", "source_key": "sha256:source-hash"},
    }
    parent_job = build_parent_summary_job(
        parent={
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "parent_id": "parent-1",
            "text": "Useful parent text.",
            "source_hash": "parent-source",
        },
        doc=doc,
        corpus={"default_ingestion_config": {}},
    )
    document_job = build_document_summary_job(
        doc=doc,
        corpus={"default_ingestion_config": {}},
        required_parent_count=1,
        summarized_parent_count=1,
    )

    assert parent_job["stage_identity"]["identity_version"] == "stage_identity.v1"
    assert parent_job["stage_identity"]["source_file_hash"] == "source-hash"
    assert parent_job["stage_identity"]["source_hash"] == "parent-source"
    assert parent_job["stage_identity"]["summary_contract_hash"] == parent_job["summary_contract_hash"]
    assert document_job["stage_identity"]["source_file_hash"] == "source-hash"
    assert document_job["stage_identity"]["summary_contract_hash"] == document_job["summary_contract_hash"]


def test_document_summary_status_requires_complete_parent_summary_context():
    assert classify_document_summary_status(
        required_parent_count=0,
        summarized_parent_count=0,
    ) == ("blocked_no_parent_summaries", "no_parent_summary_context")
    assert classify_document_summary_status(
        required_parent_count=3,
        summarized_parent_count=2,
    ) == ("blocked_parent_summaries_incomplete", "parent_summaries_incomplete")
    assert classify_document_summary_status(
        required_parent_count=3,
        summarized_parent_count=3,
    ) == ("queued", "missing_document_summary")


def test_document_summary_job_records_parent_coverage():
    job = build_document_summary_job(
        doc={"corpus_id": "corpus-1", "doc_id": "doc-1", "filename": "book.md"},
        corpus={"default_ingestion_config": {}},
        required_parent_count=5,
        summarized_parent_count=4,
    )

    assert job["kind"] == "document_summary"
    assert job["status"] == "blocked_parent_summaries_incomplete"
    assert job["missing_parent_count"] == 1


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
    def __init__(
        self,
        rows=None,
        *,
        find_one=None,
        required=0,
        summarized=0,
        aggregate_rows=None,
    ):
        self.rows = rows or []
        self.find_one_row = find_one
        self.required = required
        self.summarized = summarized
        self.aggregate_rows = aggregate_rows
        self.bulk_ops = []
        self.last_aggregate_pipeline = None

    async def find_one(self, query=None, *_args, **_kwargs):
        if self.find_one_row is not None:
            return self.find_one_row
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
                    if "$exists" in value:
                        exists = current is not None
                        if bool(value["$exists"]) != exists:
                            matched = False
                            break
                    if "$nin" in value and current in value["$nin"]:
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

    async def count_documents(self, query):
        return self.summarized if "summary" in str(query) else self.required

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

    def aggregate(self, pipeline, *_args, **_kwargs):
        self.last_aggregate_pipeline = pipeline
        return _Cursor(self.aggregate_rows or [])


class _Db:
    def __init__(
        self,
        *,
        parent_rows=None,
        doc_rows=None,
        required=0,
        summarized=0,
        doc_aggregate_rows=None,
    ):
        self.collections = {
            "corpora": _Collection(
                find_one={
                    "corpus_id": "corpus-1",
                    "default_ingestion_config": {
                        "summary_models": [{"model": "deepseek/deepseek-v4-flash"}]
                    },
                }
            ),
            "parent_chunks": _Collection(
                parent_rows or [],
                required=required,
                summarized=summarized,
            ),
            "documents": _Collection(doc_rows or [], aggregate_rows=doc_aggregate_rows),
            "summary_jobs": _Collection(),
            "summary_tree": _Collection(),
        }

    def __getitem__(self, name):
        return self.collections[name]


@pytest.mark.asyncio
async def test_plan_summary_jobs_materializes_missing_parent_jobs():
    db = _Db(
        parent_rows=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "parent_id": "parent-1",
                "text": "Useful parent text.",
            }
        ],
        doc_rows=[{"corpus_id": "corpus-1", "doc_id": "doc-1", "user_id": "user-1"}],
    )

    result = await plan_summary_jobs(
        db,
        corpus_id="corpus-1",
        apply=True,
        limit=10,
        kinds=["retrieval_parent_summary"],
    )

    assert result["status"] == "complete"
    assert result["planned"] == 1
    assert result["counts"] == {"queued": 1}
    assert result["kind_counts"] == {"retrieval_parent_summary": 1}
    assert [type(op).__name__ for op in db["summary_jobs"].bulk_ops].count("UpdateOne") == 1
    assert [type(op).__name__ for op in db["summary_jobs"].bulk_ops].count("UpdateMany") == 1
    assert result["superseded"] == 0


@pytest.mark.asyncio
async def test_backfill_summary_stage_identity_repairs_legacy_parent_jobs():
    db = _Db(
        parent_rows=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "parent_id": "parent-1",
                "text": "Useful parent text.",
                "source_hash": "parent-source",
            }
        ],
        doc_rows=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "user_id": "user-1",
                "filename": "book.md",
                "source_identity": {
                    "content_sha256": "source-hash",
                    "source_key": "sha256:source-hash",
                },
            }
        ],
    )
    db["summary_jobs"].rows = [
        {
            "job_id": "legacy-summary-job",
            "kind": "retrieval_parent_summary",
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "parent_id": "parent-1",
            "user_id": "user-1",
            "status": "queued",
        }
    ]

    dry = await backfill_summary_stage_identity(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        apply=False,
    )

    assert dry["status"] == "planned"
    assert dry["scanned"] == 1
    assert dry["planned"] == 1

    applied = await backfill_summary_stage_identity(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        apply=True,
    )

    assert applied["status"] == "complete"
    assert applied["planned"] == 1
    assert [type(op).__name__ for op in db["summary_jobs"].bulk_ops].count("UpdateOne") == 1
    update = db["summary_jobs"].bulk_ops[-1]._doc["$set"]
    assert update["stage_identity"]["identity_version"] == "stage_identity.v1"
    assert update["stage_identity"]["source_file_hash"] == "source-hash"
    assert update["stage_identity"]["source_hash"] == "parent-source"
    assert update["summary_contract_hash"]


@pytest.mark.asyncio
async def test_plan_summary_jobs_blocks_document_when_parent_summaries_incomplete():
    db = _Db(
        doc_rows=[{"corpus_id": "corpus-1", "doc_id": "doc-1", "user_id": "user-1"}],
        required=3,
        summarized=2,
    )

    result = await plan_summary_jobs(
        db,
        corpus_id="corpus-1",
        apply=False,
        limit=10,
        kinds=["document_summary"],
    )

    assert result["status"] == "planned"
    assert result["planned"] == 1
    assert result["counts"] == {"blocked_parent_summaries_incomplete": 1}
    assert result["jobs"][0]["missing_parent_count"] == 1


@pytest.mark.asyncio
async def test_plan_summary_jobs_queues_profile_only_document_summary_drift():
    db = _Db(
        doc_aggregate_rows=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "user_id": "user-1",
                "filename": "book.md",
                "content_sha256": "source-hash",
            }
        ],
        required=2,
        summarized=2,
    )

    result = await plan_summary_jobs(
        db,
        corpus_id="corpus-1",
        apply=False,
        limit=10,
        kinds=["document_summary"],
    )

    assert result["status"] == "planned"
    assert result["planned"] == 1
    assert result["counts"] == {"queued": 1}
    assert result["jobs"][0]["kind"] == "document_summary"
    first_match = db["documents"].last_aggregate_pipeline[0]["$match"]
    assert first_match["$and"][0]["ingest_stage"]["$nin"] == ["skipped_duplicate"]


@pytest.mark.asyncio
async def test_run_summary_jobs_executes_parent_runner_and_reconciles_from_parent_artifact():
    parent = {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "parent_id": "parent-1",
        "text": "Useful parent text.",
    }
    db = _Db(parent_rows=[parent])
    job = build_parent_summary_job(
        parent=parent,
        doc={"user_id": "user-1", "filename": "book.md"},
        corpus={"default_ingestion_config": {}},
    )
    db["summary_jobs"].rows = [{**job, "updated_at": 1, "attempt_count": 0}]

    async def parent_runner(*, limit, doc_ids):
        assert limit == 1
        assert doc_ids == ["doc-1"]
        db["parent_chunks"].rows[0]["summary"] = "A stored retrieval summary."
        return {"status": "healthy", "generated": 1, "indexed": 1}

    result = await run_summary_jobs(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        parent_runner=parent_runner,
    )

    assert result["status"] == "complete"
    assert result["claimed"] == 1
    assert result["parent_claimed"] == 1
    assert result["counts"] == {"succeeded": 1}
    assert result["jobs"][0]["reason"] == "summary_present"


@pytest.mark.asyncio
async def test_run_summary_jobs_executes_document_runner_after_parent_context_exists():
    doc = {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "user_id": "user-1",
        "filename": "book.md",
        "doc_profile": {},
    }
    db = _Db(doc_rows=[doc], required=2, summarized=2)
    job = build_document_summary_job(
        doc=doc,
        corpus={"default_ingestion_config": {}},
        required_parent_count=2,
        summarized_parent_count=2,
    )
    db["summary_jobs"].rows = [{**job, "updated_at": 1, "attempt_count": 0}]

    async def document_runner(*, limit, doc_ids):
        assert limit == 1
        assert doc_ids == ["doc-1"]
        db["documents"].rows[0]["doc_profile"]["summary"] = "Document-level summary."
        db["summary_tree"].rows.append(
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "node_type": "document",
                "summary": "Document-level summary.",
            }
        )
        return {"status": "complete", "built": 1}

    result = await run_summary_jobs(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        document_runner=document_runner,
    )

    assert result["status"] == "complete"
    assert result["claimed"] == 1
    assert result["document_claimed"] == 1
    assert result["counts"] == {"succeeded": 1}
    assert result["jobs"][0]["reason"] == "document_summary_present"


@pytest.mark.asyncio
async def test_run_summary_jobs_pressure_pause_keeps_parent_job_queued():
    parent = {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "parent_id": "parent-1",
        "text": "Useful parent text.",
    }
    db = _Db(parent_rows=[parent])
    job = build_parent_summary_job(
        parent=parent,
        doc={"user_id": "user-1", "filename": "book.md"},
        corpus={"default_ingestion_config": {}},
    )
    db["summary_jobs"].rows = [{**job, "updated_at": 1, "attempt_count": 0}]

    async def parent_runner(*, limit, doc_ids):
        return {"status": "paused_pressure", "generated": 0}

    result = await run_summary_jobs(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        parent_runner=parent_runner,
    )

    assert result["status"] == "paused_pressure"
    assert result["counts"] == {"queued": 1}
    assert result["jobs"][0]["reason"] == "paused_pressure"

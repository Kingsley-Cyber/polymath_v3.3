import pytest

from services.ingestion.source_parse_jobs import (
    backfill_source_parse_stage_identity,
    build_source_parse_job,
    classify_source_parse_status,
    plan_source_parse_jobs,
    run_source_parse_jobs,
    source_parse_job_id,
)


def test_source_parse_job_id_is_deterministic():
    first = source_parse_job_id(
        corpus_id="c",
        batch_id="b",
        item_id="i",
        source_fingerprint="source",
        contract_hash="contract",
    )
    second = source_parse_job_id(
        corpus_id="c",
        batch_id="b",
        item_id="i",
        source_fingerprint="source",
        contract_hash="contract",
    )

    assert first == second
    assert first.startswith("source_parse_")


def test_source_parse_blocks_when_source_disappears(tmp_path):
    missing = tmp_path / "missing.pdf"
    status, reason = classify_source_parse_status(
        {
            "status": "queued",
            "source_path": str(missing),
            "filename": "missing.pdf",
        }
    )

    assert status == "blocked_source_missing"
    assert reason == "source_missing"


def test_source_parse_succeeds_once_document_or_parsed_stage_exists():
    status, reason = classify_source_parse_status(
        {
            "status": "running",
            "phase": "chunking",
            "stage": "parsed",
            "doc_id": "doc-1",
        }
    )

    assert status == "succeeded"
    assert reason == "parsed_or_document_created"


def test_build_source_parse_job_preserves_manifest_identity():
    job = build_source_parse_job(
        item={
            "corpus_id": "corpus-1",
            "batch_id": "batch-1",
            "item_id": "item-1",
            "user_id": "user-1",
            "filename": "book.pdf",
            "relative_path": "a/book.pdf",
            "source_path": "/ingest-source/a/book.pdf",
            "source_identity": {
                "content_sha256": "source-hash",
                "source_key": "sha256:source-hash",
            },
            "status": "queued",
            "phase": "queued",
            "size_bytes": 12,
        },
        batch={
            "batch_id": "batch-1",
            "source": "local_folder",
            "root_path": "/ingest-source",
            "status": "running",
            "options": {"profile": "rtx_assisted"},
        },
    )

    assert job["kind"] == "source_parse"
    assert job["corpus_id"] == "corpus-1"
    assert job["batch_id"] == "batch-1"
    assert job["item_id"] == "item-1"
    assert job["status"] in {"queued", "blocked_source_missing"}
    assert job["source_parse_contract"]["profile"] == "rtx_assisted"
    assert job["stage_identity"]["identity_version"] == "stage_identity.v1"
    assert job["stage_identity"]["source_file_hash"] == "source-hash"
    assert job["stage_identity"]["source_key"] == "sha256:source-hash"
    assert job["stage_identity"]["source_fingerprint"] == job["source_fingerprint"]
    assert (
        job["stage_identity"]["source_parse_contract_hash"]
        == job["source_parse_contract_hash"]
    )


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self._limit = len(rows)

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        self._limit = value
        return self

    async def to_list(self, length=None):
        limit = self._limit if length is None else min(self._limit, length)
        return self.rows[:limit]


class _UpdateResult:
    def __init__(self, modified_count=0):
        self.modified_count = modified_count


class _Collection:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.bulk_ops = []
        self.update_many_calls = []

    def find(self, query=None, *_args, **_kwargs):
        query = query or {}
        return _Cursor([row for row in self.rows if _matches(row, query)])

    async def bulk_write(self, ops, **_kwargs):
        self.bulk_ops.extend(ops)

    async def update_many(self, query, update, **_kwargs):
        self.update_many_calls.append((query, update))
        modified = 0
        for row in self.rows:
            if not _matches(row, query):
                continue
            _apply_update(row, update)
            modified += 1
        return _UpdateResult(modified)

    async def update_one(self, query, update, **_kwargs):
        for row in self.rows:
            if not _matches(row, query):
                continue
            _apply_update(row, update)
            return _UpdateResult(1)
        return _UpdateResult(0)

    def aggregate(self, *_args, **_kwargs):
        return _Cursor([])


class _Db:
    def __init__(self):
        self.collections = {
            "ingest_batches": _Collection(
                [
                    {
                        "batch_id": "batch-1",
                        "corpus_id": "corpus-1",
                        "user_id": "user-1",
                        "source": "local_folder",
                        "root_path": "/ingest-source",
                        "status": "running",
                        "options": {"profile": "rtx_assisted"},
                    }
                ]
            ),
            "ingest_batch_items": _Collection(
                [
                    {
                        "corpus_id": "corpus-1",
                        "batch_id": "batch-1",
                        "item_id": "item-1",
                        "user_id": "user-1",
                        "filename": "book.pdf",
                        "relative_path": "book.pdf",
                        "source_identity": {"content_sha256": "source-hash"},
                        "source_path": "book.pdf",
                        "status": "queued",
                        "phase": "queued",
                        "ordinal": 0,
                    },
                    {
                        "corpus_id": "corpus-1",
                        "batch_id": "cancelled-batch",
                        "item_id": "item-2",
                        "user_id": "user-1",
                        "filename": "ignored.pdf",
                        "relative_path": "ignored.pdf",
                        "source_path": "ignored.pdf",
                        "status": "queued",
                        "phase": "queued",
                        "ordinal": 1,
                    },
                ]
            ),
            "source_parse_jobs": _Collection(),
        }

    def __getitem__(self, name):
        return self.collections[name]


def _matches(row, query):
    for key, expected in (query or {}).items():
        if key == "$or":
            if not any(_matches(row, branch) for branch in expected):
                return False
            continue
        value = row.get(key)
        if isinstance(expected, dict):
            if "$in" in expected and value not in expected["$in"]:
                return False
            if "$nin" in expected and value in expected["$nin"]:
                return False
            if "$lte" in expected and (value is None or value > expected["$lte"]):
                return False
            if "$exists" in expected:
                exists = key in row
                if bool(expected["$exists"]) != exists:
                    return False
        elif value != expected:
            return False
    return True


def _apply_update(row, update):
    for key, value in (update.get("$set") or {}).items():
        row[key] = value
    for key in (update.get("$unset") or {}):
        row.pop(key, None)
    for key, value in (update.get("$inc") or {}).items():
        row[key] = int(row.get(key) or 0) + int(value)


@pytest.mark.asyncio
async def test_plan_source_parse_jobs_materializes_manifest_rows():
    db = _Db()

    result = await plan_source_parse_jobs(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        apply=True,
        limit=10,
    )

    assert result["status"] == "complete"
    assert result["planned"] == 1
    assert result["counts"] == {"queued": 1}
    assert result["kind_counts"] == {"source_parse": 1}
    assert [type(op).__name__ for op in db["source_parse_jobs"].bulk_ops].count("UpdateOne") == 1
    assert [type(op).__name__ for op in db["source_parse_jobs"].bulk_ops].count("UpdateMany") == 1
    assert result["superseded"] == 0


@pytest.mark.asyncio
async def test_backfill_source_parse_stage_identity_repairs_legacy_jobs():
    db = _Db()
    db["source_parse_jobs"].rows = [
        {
            "job_id": "legacy-job",
            "corpus_id": "corpus-1",
            "batch_id": "batch-1",
            "item_id": "item-1",
            "user_id": "user-1",
            "status": "succeeded",
        }
    ]

    dry = await backfill_source_parse_stage_identity(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        apply=False,
    )

    assert dry["status"] == "planned"
    assert dry["scanned"] == 1
    assert dry["planned"] == 1
    assert dry["samples"][0]["job_id"] == "legacy-job"

    applied = await backfill_source_parse_stage_identity(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        apply=True,
    )

    assert applied["status"] == "complete"
    assert applied["planned"] == 1
    assert [type(op).__name__ for op in db["source_parse_jobs"].bulk_ops].count("UpdateOne") == 1
    update = db["source_parse_jobs"].bulk_ops[-1]._doc["$set"]
    assert update["stage_identity"]["identity_version"] == "stage_identity.v1"
    assert update["stage_identity"]["source_file_hash"] == "source-hash"
    assert update["source_parse_contract"]["profile"] == "rtx_assisted"


@pytest.mark.asyncio
async def test_run_source_parse_jobs_resumes_batches_through_runner(monkeypatch):
    db = _Db()
    db["source_parse_jobs"].rows = [
        {
            "corpus_id": "corpus-1",
            "user_id": "user-1",
            "batch_id": "batch-1",
            "item_id": "item-1",
            "job_id": "job-1",
            "status": "queued",
        }
    ]
    calls = []

    async def fake_reconcile(_db, *, batch_id, user_id):
        calls.append(("reconcile", batch_id, user_id))
        return {"reconciled_items": 0}

    async def fake_refresh(_db, batch_id, *, user_id=None):
        calls.append(("refresh", batch_id, user_id))
        return {"batch_id": batch_id, "status": "queued"}

    def fake_start(*, db, ingestion_service, batch_id, user_id):
        calls.append(("start", batch_id, user_id, ingestion_service))
        return True

    monkeypatch.setattr("services.ingestion.batches.reconcile_stale_items", fake_reconcile)
    monkeypatch.setattr("services.ingestion.batches.refresh_batch_counts", fake_refresh)
    monkeypatch.setattr("services.ingestion.batches.start_local_batch_runner", fake_start)

    result = await run_source_parse_jobs(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        ingestion_service=object(),
        limit=5,
        start_runners=True,
    )

    assert result["status"] == "started"
    assert result["requested"] == 1
    assert result["claimed"] == 1
    assert result["eligible_items"] == 1
    assert result["batch_count"] == 1
    assert result["runners_started"] == 1
    assert result["runner_deferred"] is False
    assert db["source_parse_jobs"].rows[0]["status"] == "running"
    assert db["source_parse_jobs"].rows[0]["runner"] == "source_parse_jobs.run"
    assert db["source_parse_jobs"].rows[0]["runner_deferred"] is False
    assert ("reconcile", "batch-1", "user-1") in calls
    assert any(call[0] == "start" for call in calls)


@pytest.mark.asyncio
async def test_run_source_parse_jobs_can_defer_to_ingest_worker(monkeypatch):
    db = _Db()
    db["source_parse_jobs"].rows = [
        {
            "corpus_id": "corpus-1",
            "user_id": "user-1",
            "batch_id": "batch-1",
            "item_id": "item-1",
            "job_id": "job-1",
            "status": "queued",
        }
    ]

    async def fake_reconcile(_db, *, batch_id, user_id):
        return {"reconciled_items": 0}

    async def fake_refresh(_db, batch_id, *, user_id=None):
        return {"batch_id": batch_id, "status": "queued"}

    def fail_start(**_kwargs):
        raise AssertionError("public API must not start runners when disabled")

    monkeypatch.setattr("services.ingestion.batches.reconcile_stale_items", fake_reconcile)
    monkeypatch.setattr("services.ingestion.batches.refresh_batch_counts", fake_refresh)
    monkeypatch.setattr("services.ingestion.batches.start_local_batch_runner", fail_start)

    result = await run_source_parse_jobs(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        ingestion_service=None,
        limit=5,
        start_runners=False,
    )

    assert result["status"] == "deferred"
    assert result["claimed"] == 0
    assert result["runner_deferred"] is True
    assert result["runners_started"] == 0
    assert db["source_parse_jobs"].rows[0]["status"] == "queued"


@pytest.mark.asyncio
async def test_run_source_parse_jobs_does_not_take_already_claimed_rows(monkeypatch):
    db = _Db()
    db["source_parse_jobs"].rows = [
        {
            "corpus_id": "corpus-1",
            "user_id": "user-1",
            "batch_id": "batch-1",
            "item_id": "item-1",
            "job_id": "job-1",
            "status": "queued",
        }
    ]

    async def fake_reconcile(_db, *, batch_id, user_id):
        return {"reconciled_items": 0}

    async def fake_refresh(_db, batch_id, *, user_id=None):
        return {"batch_id": batch_id, "status": "queued"}

    def fake_start(**_kwargs):
        return True

    original_update_one = db["source_parse_jobs"].update_one

    async def racing_update_one(query, update, **kwargs):
        db["source_parse_jobs"].rows[0]["status"] = "running"
        return await original_update_one(query, update, **kwargs)

    monkeypatch.setattr("services.ingestion.batches.reconcile_stale_items", fake_reconcile)
    monkeypatch.setattr("services.ingestion.batches.refresh_batch_counts", fake_refresh)
    monkeypatch.setattr("services.ingestion.batches.start_local_batch_runner", fake_start)
    monkeypatch.setattr(db["source_parse_jobs"], "update_one", racing_update_one)

    result = await run_source_parse_jobs(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        ingestion_service=object(),
        limit=5,
        start_runners=True,
    )

    assert result["status"] == "empty"
    assert result["candidates"] == 1
    assert result["claimed"] == 0
    assert result["runners_started"] == 0

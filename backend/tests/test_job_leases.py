from datetime import datetime, timedelta

import pytest

from services.ingestion.job_leases import (
    SUPERSEDED_JOB_STATUS,
    claim_runnable_jobs,
    lease_deadline,
    reclaim_expired_running_jobs,
    retire_superseded_jobs,
)


def test_lease_deadline_uses_minimum_one_minute():
    now = datetime(2026, 1, 1, 12, 0, 0)

    assert lease_deadline(now, lease_seconds=1) == now + timedelta(seconds=60)


class _UpdateResult:
    def __init__(self, modified_count=3):
        self.modified_count = modified_count


class _BulkResult:
    modified_count = 2


class _Collection:
    def __init__(self, rows=None):
        self.query = None
        self.update = None
        self.bulk_ops = []
        self.rows = rows or []

    async def update_many(self, query, update):
        self.query = query
        self.update = update
        return _UpdateResult()

    async def update_one(self, query, update, **_kwargs):
        self.query = query
        self.update = update
        for row in self.rows:
            if not _matches(row, query):
                continue
            _apply_update(row, update)
            return _UpdateResult(1)
        return _UpdateResult(0)

    async def bulk_write(self, ops, **_kwargs):
        self.bulk_ops.extend(ops)
        return _BulkResult()


class _Db:
    def __init__(self, rows=None):
        self.collection = _Collection(rows)

    def __getitem__(self, name):
        assert name == "summary_jobs"
        return self.collection


def _matches(row, query):
    for key, expected in (query or {}).items():
        actual = row.get(key)
        if isinstance(expected, dict):
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
        elif actual != expected:
            return False
    return True


def _apply_update(row, update):
    for key, value in (update.get("$set") or {}).items():
        row[key] = value
    for key, value in (update.get("$inc") or {}).items():
        row[key] = int(row.get(key) or 0) + int(value)


@pytest.mark.asyncio
async def test_reclaim_expired_running_jobs_requeues_stale_rows():
    db = _Db()
    now = datetime(2026, 1, 1, 12, 0, 0)

    reclaimed = await reclaim_expired_running_jobs(
        db,
        collection_name="summary_jobs",
        corpus_id="corpus-1",
        user_id="user-1",
        now=now,
        lease_seconds=300,
    )

    assert reclaimed == 3
    assert db.collection.query["corpus_id"] == "corpus-1"
    assert db.collection.query["user_id"] == "user-1"
    assert db.collection.query["status"] == "running"
    assert db.collection.update["$set"]["status"] == "queued"
    assert db.collection.update["$set"]["reason"] == "lease_expired"
    assert db.collection.update["$set"]["last_reclaimed_at"] == now
    assert "lease_until" in db.collection.update["$unset"]


@pytest.mark.asyncio
async def test_retire_superseded_jobs_marks_same_artifact_old_rows():
    db = _Db()
    now = datetime(2026, 1, 1, 12, 0, 0)

    modified = await retire_superseded_jobs(
        db,
        collection_name="summary_jobs",
        jobs=[
            {
                "job_id": "new-job",
                "corpus_id": "corpus-1",
                "kind": "document_summary",
                "doc_id": "doc-1",
            }
        ],
        identity_fields=("corpus_id", "kind", "doc_id"),
        supersedable_statuses={"queued", "failed"},
        now=now,
    )

    assert modified == 2
    assert len(db.collection.bulk_ops) == 1
    op = db.collection.bulk_ops[0]
    query = op._filter
    update = op._doc
    assert query == {
        "corpus_id": "corpus-1",
        "kind": "document_summary",
        "doc_id": "doc-1",
        "job_id": {"$ne": "new-job"},
        "status": {"$in": ["failed", "queued"]},
    }
    assert update["$set"]["status"] == SUPERSEDED_JOB_STATUS
    assert update["$set"]["reason"] == "stage_identity_superseded"
    assert update["$set"]["superseded_by_job_id"] == "new-job"
    assert update["$set"]["superseded_at"] == now
    assert update["$set"]["lease_until"] is None


@pytest.mark.asyncio
async def test_claim_runnable_jobs_only_returns_atomically_claimed_rows():
    now = datetime(2026, 1, 1, 12, 0, 0)
    db = _Db(
        rows=[
            {"job_id": "job-1", "status": "queued", "attempt_count": 0},
            {"job_id": "job-2", "status": "running", "attempt_count": 0},
            {"job_id": "job-3", "status": "failed", "attempt_count": 2},
        ]
    )

    claimed = await claim_runnable_jobs(
        db,
        collection_name="summary_jobs",
        jobs=[
            {"job_id": "job-1", "status": "queued", "attempt_count": 0},
            {"job_id": "job-2", "status": "queued", "attempt_count": 0},
            {"job_id": "job-3", "status": "failed", "attempt_count": 2},
        ],
        runnable_statuses={"queued", "failed"},
        now=now,
        runner="summary_jobs.run",
        increment_attempt=True,
    )

    assert [job["job_id"] for job in claimed] == ["job-1", "job-3"]
    assert claimed[0]["status"] == "running"
    assert claimed[0]["runner"] == "summary_jobs.run"
    assert claimed[0]["attempt_count"] == 1
    assert claimed[1]["attempt_count"] == 3
    assert db.collection.rows[0]["status"] == "running"
    assert db.collection.rows[0]["lease_until"] == lease_deadline(now)
    assert db.collection.rows[1]["status"] == "running"
    assert db.collection.rows[1]["attempt_count"] == 0
    assert db.collection.rows[2]["status"] == "running"
    assert db.collection.rows[2]["attempt_count"] == 3

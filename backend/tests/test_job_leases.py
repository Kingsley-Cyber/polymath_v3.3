from datetime import datetime, timedelta

import pytest

from services.ingestion.job_leases import (
    DEAD_LETTER_JOB_STATUS,
    SUPERSEDED_JOB_STATUS,
    acquire_lane_lease,
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


@pytest.mark.asyncio
async def test_claim_dead_letters_exhausted_job_without_running_it():
    db = _Db(
        rows=[
            {
                "job_id": "job-poison",
                "status": "queued",
                "attempt_count": 5,
                "last_error": "TimeoutError: provider stalled",
            }
        ]
    )

    claimed = await claim_runnable_jobs(
        db,
        collection_name="summary_jobs",
        jobs=[dict(db.collection.rows[0])],
        runnable_statuses={"queued"},
        runner="summary_jobs.run",
        increment_attempt=True,
        max_attempts=5,
    )

    assert claimed == []
    assert db.collection.rows[0]["status"] == DEAD_LETTER_JOB_STATUS
    assert db.collection.rows[0]["failure_class"] == "timeouterror"


class _LaneResult:
    deleted_count = 1


class _LaneCollection:
    def __init__(self):
        self.row = None

    async def find_one_and_update(self, query, update, **_kwargs):
        now = update["$set"]["updated_at"]
        if self.row and self.row["lease_until"] > now and self.row["owner"] != update["$set"]["owner"]:
            return None
        self.row = {"_id": query["_id"], **update["$set"]}
        return self.row

    async def delete_one(self, query):
        if self.row and self.row.get("lease_id") == query.get("lease_id"):
            self.row = None
            return _LaneResult()
        return type("Result", (), {"deleted_count": 0})()


@pytest.mark.asyncio
async def test_lane_lease_allows_only_one_owner_until_expiry():
    collection = _LaneCollection()
    db = {"ingest_lane_leases": collection}
    now = datetime(2026, 1, 1, 12, 0, 0)

    first = await acquire_lane_lease(
        db,
        corpus_id="corpus-1",
        lane="summary",
        owner="worker-a",
        now=now,
    )
    second = await acquire_lane_lease(
        db,
        corpus_id="corpus-1",
        lane="summary",
        owner="worker-b",
        now=now + timedelta(seconds=1),
    )
    reclaimed = await acquire_lane_lease(
        db,
        corpus_id="corpus-1",
        lane="summary",
        owner="worker-b",
        now=now + timedelta(hours=1),
    )

    assert first is not None
    assert second is None
    assert reclaimed is not None
    assert reclaimed["owner"] == "worker-b"

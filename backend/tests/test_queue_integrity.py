from datetime import datetime, timedelta

import pytest
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError

from db.queue_integrity import (
    bulk_upsert_durable_jobs,
    deduplicate_job_ids,
    select_canonical_job_row,
)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return self.rows if length is None else self.rows[:length]


class _Result:
    def __init__(self, *, deleted_count=0):
        self.deleted_count = deleted_count


class _Collection:
    name = "summary_jobs"

    def __init__(self, rows):
        self.rows = rows

    def aggregate(self, _pipeline):
        groups = {}
        for row in self.rows:
            groups.setdefault(row.get("job_id"), []).append(row.get("_id"))
        return _Cursor(
            [
                {"_id": job_id, "ids": ids, "count": len(ids)}
                for job_id, ids in groups.items()
                if job_id and len(ids) > 1
            ]
        )

    def find(self, query):
        ids = set(query["_id"]["$in"])
        return _Cursor([row for row in self.rows if row.get("_id") in ids])

    async def update_one(self, query, update):
        row = next(row for row in self.rows if row.get("_id") == query.get("_id"))
        row.update(update.get("$set") or {})
        for field in update.get("$unset") or {}:
            row.pop(field, None)
        return _Result()

    async def delete_many(self, query):
        ids = set(query["_id"]["$in"])
        before = len(self.rows)
        self.rows = [row for row in self.rows if row.get("_id") not in ids]
        return _Result(deleted_count=before - len(self.rows))


def test_terminal_success_beats_expired_running_duplicate():
    now = datetime(2026, 1, 1, 12, 0, 0)
    succeeded = {
        "_id": "success",
        "job_id": "job-1",
        "status": "succeeded",
        "updated_at": now - timedelta(hours=1),
    }
    expired = {
        "_id": "running",
        "job_id": "job-1",
        "status": "running",
        "lease_until": now - timedelta(minutes=1),
        "updated_at": now,
    }

    assert select_canonical_job_row(
        "summary_jobs",
        [expired, succeeded],
        now=now,
    ) is succeeded


def test_promoted_extraction_beats_retryable_duplicate():
    winner = select_canonical_job_row(
        "extraction_jobs",
        [
            {"_id": "retry", "status": "provider_failed", "attempt_count": 5},
            {"_id": "promoted", "status": "promoted", "attempt_count": 1},
        ],
        now=datetime(2026, 1, 1),
    )

    assert winner["_id"] == "promoted"


@pytest.mark.asyncio
async def test_deduplicate_job_ids_keeps_success_and_merges_attempt_count():
    now = datetime(2026, 1, 1, 12, 0, 0)
    collection = _Collection(
        [
            {
                "_id": "success",
                "job_id": "job-1",
                "status": "succeeded",
                "attempt_count": 1,
                "created_at": now - timedelta(hours=2),
            },
            {
                "_id": "running",
                "job_id": "job-1",
                "status": "running",
                "attempt_count": 4,
                "lease_until": now - timedelta(minutes=1),
                "created_at": now - timedelta(hours=1),
            },
        ]
    )

    result = await deduplicate_job_ids(collection, now=now)

    assert result["reconciled_groups"] == 1
    assert result["removed_rows"] == 1
    assert len(collection.rows) == 1
    assert collection.rows[0]["_id"] == "success"
    assert collection.rows[0]["status"] == "succeeded"
    assert collection.rows[0]["attempt_count"] == 4
    assert collection.rows[0]["lease_until"] is None


@pytest.mark.asyncio
async def test_deduplicate_job_ids_requeues_expired_winner():
    now = datetime(2026, 1, 1, 12, 0, 0)
    collection = _Collection(
        [
            {
                "_id": "older",
                "job_id": "job-1",
                "status": "queued",
                "attempt_count": 0,
                "updated_at": now - timedelta(hours=1),
            },
            {
                "_id": "expired",
                "job_id": "job-1",
                "status": "running",
                "attempt_count": 2,
                "lease_until": now - timedelta(minutes=1),
                "updated_at": now,
                "runner": "dead-worker",
            },
        ]
    )

    await deduplicate_job_ids(collection, now=now)

    assert collection.rows[0]["status"] == "queued"
    assert collection.rows[0]["reason"] == "lease_expired_during_queue_dedup"
    assert "runner" not in collection.rows[0]


@pytest.mark.asyncio
async def test_bulk_upsert_replays_duplicate_key_race_as_updates():
    class _RacingCollection:
        name = "summary_jobs"

        def __init__(self):
            self.calls = []

        async def bulk_write(self, ops, ordered=False):
            self.calls.append((ops, ordered))
            if len(self.calls) == 1:
                raise BulkWriteError({"writeErrors": [{"code": 11000}]})
            return _Result()

    collection = _RacingCollection()
    await bulk_upsert_durable_jobs(
        collection,
        [UpdateOne({"job_id": "job-1"}, {"$set": {"status": "queued"}}, upsert=True)],
    )

    assert len(collection.calls) == 2
    retry_op = collection.calls[1][0][0]
    assert retry_op._filter == {"job_id": "job-1"}
    assert retry_op._upsert is False

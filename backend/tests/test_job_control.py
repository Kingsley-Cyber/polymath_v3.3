from __future__ import annotations

import pytest

from services.ingestion.job_control import control_job


class _Result:
    modified_count = 1


class _Collection:
    def __init__(self, row):
        self.row = row
        self.update = None

    async def find_one(self, query, projection=None):
        if self.row.get("corpus_id") == query.get("corpus_id") and self.row.get(
            "job_id"
        ) == query.get("job_id"):
            return dict(self.row)
        return None

    async def update_one(self, query, update):
        self.update = update
        self.row.update(update.get("$set") or {})
        for key in update.get("$unset") or {}:
            self.row.pop(key, None)
        return _Result()


class _Db:
    def __init__(self, row):
        self.collection = _Collection(row)

    def __getitem__(self, name):
        assert name == "summary_jobs"
        return self.collection


@pytest.mark.asyncio
async def test_operator_retry_is_only_path_that_resets_dead_letter_attempts() -> None:
    db = _Db(
        {
            "corpus_id": "corpus-1",
            "job_id": "job-1",
            "status": "dead_letter",
            "attempt_count": 5,
            "operator_override_generation": 2,
        }
    )

    result = await control_job(
        db,
        corpus_id="corpus-1",
        lane="summary",
        job_id="job-1",
        action="retry",
        reason="Provider contract changed",
        operator_user_id="user-1",
    )

    assert result["status"] == "queued"
    assert result["operator_override_generation"] == 3
    assert db.collection.row["attempt_count"] == 0
    assert db.collection.row["status"] == "queued"

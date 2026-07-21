import pytest

from services.ingestion.graph_promotion_jobs import (
    backfill_promoted_extraction_marks,
    classify_graph_promotion_candidate,
    extraction_artifact_id,
    graph_gap_reason,
    graph_job_id,
    graph_promotion_contract_hash,
    mark_doc_extractions_promoted,
    run_graph_promotion_jobs,
    _corpus_graph_required,
    _promoted_doc_unmarked_extraction_pipeline,
    _unpromoted_doc_id_pipeline,
)


class _FakeUpdateCollection:
    def __init__(self, modified_count=0):
        self.modified_count = modified_count
        self.update_many_calls = []

    async def update_many(self, query, update):
        self.update_many_calls.append((query, update))
        return type("Result", (), {"modified_count": self.modified_count})()


class _FakeAggregateUpdateCollection(_FakeUpdateCollection):
    def __init__(self, rows, modified_count=0):
        super().__init__(modified_count=modified_count)
        self.rows = list(rows)
        self.aggregate_calls = []

    def aggregate(self, pipeline):
        self.aggregate_calls.append(pipeline)
        return _FakeCursor(self.rows)


class _FakeDb(dict):
    def __getitem__(self, name):
        return dict.__getitem__(self, name)


class _FindOneCollection:
    def __init__(self, row):
        self.row = row

    async def find_one(self, *_args, **_kwargs):
        return self.row


class _FakeQueuedJobsCollection:
    def __init__(self, jobs):
        self.jobs = list(jobs)
        self.update_one_calls = []
        self.update_many_calls = []

    def find(self, query, projection=None):
        rows = [
            {key: value for key, value in job.items() if key != "_id"}
            for job in self.jobs
            if job.get("corpus_id") == query.get("corpus_id")
            and job.get("status") == query.get("status")
        ]
        return _FakeCursor(rows)

    async def update_one(self, query, update):
        self.update_one_calls.append((query, update))
        modified = 0
        for job in self.jobs:
            if all(job.get(key) == value for key, value in query.items()):
                if "$set" in update:
                    job.update(update["$set"])
                if "$inc" in update:
                    for key, value in update["$inc"].items():
                        job[key] = int(job.get(key) or 0) + int(value or 0)
                modified = 1
                break
        return type("Result", (), {"modified_count": modified})()

    async def update_many(self, query, update):
        self.update_many_calls.append((query, update))
        return type("Result", (), {"modified_count": 0})()


class _FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        self.rows = self.rows[:value]
        return self

    async def to_list(self, length=None):
        return list(self.rows if length is None else self.rows[:length])


def test_graph_gap_reason_detects_missing_neo4j():
    assert (
        graph_gap_reason({"write_state": {"qdrant_written": True, "neo4j_written": False}})
        == "neo4j_missing"
    )


def test_graph_gap_reason_detects_verifier_mismatch():
    assert (
        graph_gap_reason(
            {
                "write_state": {
                    "neo4j_written": True,
                    "verified": False,
                    "verify_errors": ["neo4j HAS_CHUNK mismatch"],
                }
            }
        )
        == "neo4j_verify_mismatch"
    )


def test_graph_promotion_candidate_queues_clean_staged_graph_gap():
    candidate = classify_graph_promotion_candidate(
        {
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "source_identity": {"content_sha256": "source-hash", "source_key": "sha256:source-hash"},
            "write_state": {"qdrant_written": True, "neo4j_written": False},
            "ingestion_config": {"use_neo4j": True},
            "staged_extractions": 12,
            "extraction_artifact_ids": ["sha256:b", "sha256:a", "sha256:a"],
            "ghost_b_failure_count": 0,
            "failure_rows": 0,
        }
    )

    assert candidate is not None
    assert candidate["status"] == "queued"
    assert candidate["reason"] == "neo4j_missing"
    assert candidate["extraction_artifact_ids"] == ["sha256:a", "sha256:b"]
    assert candidate["extraction_artifact_count"] == 2
    assert candidate["graph_contract_hash"]
    assert candidate["stage_identity"]["source_file_hash"] == "source-hash"
    assert candidate["stage_identity"]["extraction_artifact_ids"] == ["sha256:a", "sha256:b"]
    assert candidate["stage_identity"]["graph_contract_hash"] == candidate["graph_contract_hash"]


def test_graph_promotion_candidate_queues_good_rows_even_with_failed_chunks():
    candidate = classify_graph_promotion_candidate(
        {
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "write_state": {"qdrant_written": True, "neo4j_written": False},
            "ingestion_config": {"use_neo4j": True},
            "staged_extractions": 12,
            "ghost_b_failure_count": 2,
            "failure_rows": 2,
        }
    )

    assert candidate is not None
    assert candidate["status"] == "queued"
    assert candidate["failed_chunks"] == 2
    assert candidate["failure_rows"] == 2


def test_graph_promotion_candidate_blocks_missing_extractions():
    candidate = classify_graph_promotion_candidate(
        {
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "write_state": {"qdrant_written": True, "neo4j_written": False},
            "ingestion_config": {"use_neo4j": True},
            "staged_extractions": 0,
            "ghost_b_failure_count": 0,
            "failure_rows": 0,
        }
    )

    assert candidate is not None
    assert candidate["status"] == "blocked_no_extractions"


def test_graph_promotion_candidate_ignores_promoted_artifact_metadata_gap():
    candidate = classify_graph_promotion_candidate(
        {
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "source_identity": {"content_sha256": "source-hash"},
            "write_state": {"qdrant_written": True, "neo4j_written": True, "verified": True},
            "ingestion_config": {"use_neo4j": True},
            "staged_extractions": 2,
            "unpromoted_extractions": 2,
            "ghost_b_failure_count": 0,
            "failure_rows": 0,
        }
    )

    assert candidate is None


@pytest.mark.asyncio
async def test_backfill_promoted_extraction_marks_stamps_legacy_rows():
    ghost_rows = [{"doc_id": "doc-1", "rows": 3, "filename": "Book.md"}]
    ghost = _FakeAggregateUpdateCollection(ghost_rows, modified_count=3)
    jobs = _FakeUpdateCollection(modified_count=2)
    db = _FakeDb({"ghost_b_extractions": ghost, "extraction_jobs": jobs})

    result = await backfill_promoted_extraction_marks(
        db,
        corpus_id="corpus-1",
        apply=True,
        limit=5,
    )

    assert result["status"] == "applied"
    assert result["planned_docs"] == 1
    assert result["planned_rows"] == 3
    assert result["modified_docs"] == 1
    assert result["modified_rows"] == 3
    assert ghost.update_many_calls[0][0] == {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "status": "ok",
    }
    assert jobs.update_many_calls[0][0] == {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "status": "succeeded",
    }


def test_promoted_doc_unmarked_extraction_pipeline_requires_graph_written_doc():
    pipeline = _promoted_doc_unmarked_extraction_pipeline(corpus_id="corpus-1", limit=5)

    lookup = next(stage["$lookup"] for stage in pipeline if "$lookup" in stage)
    match = lookup["pipeline"][0]["$match"]
    assert match["write_state.qdrant_written"] is True
    assert match["write_state.neo4j_written"] is True


def test_graph_job_id_is_deterministic():
    assert graph_job_id(corpus_id="c", doc_id="d", reason="r") == graph_job_id(
        corpus_id="c", doc_id="d", reason="r"
    )


def test_graph_promotion_contract_hash_tracks_graph_contract():
    first = graph_promotion_contract_hash(
        {
            "write_state": {"neo4j_written": False},
            "ingestion_config": {"use_neo4j": True},
        }
    )
    second = graph_promotion_contract_hash(
        {
            "write_state": {"neo4j_written": False},
            "ingestion_config": {"use_neo4j": False},
        }
    )

    assert first != second


def test_extraction_artifact_id_uses_explicit_or_derived_fingerprint():
    assert extraction_artifact_id({"raw_output_artifact_id": "sha256:raw"}) == "sha256:raw"
    derived = extraction_artifact_id(
        {
            "doc_id": "doc-1",
            "chunk_id": "chunk-1",
            "chunk_hash": "chunk-hash",
            "extraction_contract_hash": "contract",
            "raw_output_fingerprint": {"sha256": "raw"},
            "status": "ok",
        }
    )

    assert derived.startswith("derived:")
    assert derived == extraction_artifact_id(
        {
            "doc_id": "doc-1",
            "chunk_id": "chunk-1",
            "chunk_hash": "chunk-hash",
            "extraction_contract_hash": "contract",
            "raw_output_fingerprint": {"sha256": "raw"},
            "status": "ok",
        }
    )


def test_unpromoted_doc_id_pipeline_groups_before_limit():
    pipeline = _unpromoted_doc_id_pipeline(corpus_id="corpus-1", limit=25)

    stages = [next(iter(stage)) for stage in pipeline]
    assert stages == ["$match", "$group", "$sort", "$limit"]
    assert pipeline[0]["$match"]["corpus_id"] == "corpus-1"
    assert pipeline[1]["$group"] == {"_id": "$doc_id"}
    assert pipeline[-1]["$limit"] == 25


@pytest.mark.asyncio
async def test_corpus_graph_required_uses_current_corpus_contract():
    assert await _corpus_graph_required(
        _FakeDb({"corpora": _FindOneCollection({"default_ingestion_config": {"use_neo4j": False}})}),
        corpus_id="corpus-1",
    ) is False
    assert await _corpus_graph_required(
        _FakeDb({"corpora": _FindOneCollection({"default_ingestion_config": {"use_neo4j": True}})}),
        corpus_id="corpus-1",
    ) is True


@pytest.mark.asyncio
async def test_mark_doc_extractions_promoted_stamps_rows_and_jobs():
    ghost_rows = _FakeUpdateCollection(modified_count=3)
    jobs = _FakeUpdateCollection(modified_count=2)
    db = _FakeDb(
        {
            "ghost_b_extractions": ghost_rows,
            "extraction_jobs": jobs,
        }
    )

    result = await mark_doc_extractions_promoted(
        db,
        corpus_id="corpus-1",
        doc_id="doc-1",
        graph_job_id="graph-job-1",
        result={
            "status": "flushed_to_neo4j",
            "neo4j_flushed": True,
            "staged_results_written": 3,
        },
    )

    assert result == {"ghost_b_rows_promoted": 3, "extraction_jobs_promoted": 2}
    assert ghost_rows.update_many_calls[0][0] == {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "status": "ok",
    }
    assert jobs.update_many_calls[0][0] == {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "status": "succeeded",
    }
    job_set = jobs.update_many_calls[0][1]["$set"]
    assert job_set["status"] == "promoted"
    assert job_set["reason"] == "graph_promoted"
    assert job_set["graph_promotion_job_id"] == "graph-job-1"
    assert job_set["graph_promotion_result"]["staged_results_written"] == 3


@pytest.mark.asyncio
async def test_run_graph_promotion_jobs_marks_partial_flush_promoted(monkeypatch):
    jobs = _FakeQueuedJobsCollection(
        [
            {
                "job_id": "graph-job-1",
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "user_id": "user-1",
                "status": "queued",
            }
        ]
    )
    ghost_rows = _FakeUpdateCollection(modified_count=7)
    extraction_jobs = _FakeUpdateCollection(modified_count=6)
    db = _FakeDb(
        {
            "graph_promotion_jobs": jobs,
            "ghost_b_extractions": ghost_rows,
            "extraction_jobs": extraction_jobs,
        }
    )

    async def fake_backfill_failed_graph_chunks(**_kwargs):
        assert _kwargs["allow_extraction"] is False
        return {
            "status": "done",
            "doc_id": "doc-1",
            "corpus_id": "corpus-1",
            "remaining_failed_chunks": 2,
            "neo4j_flushed": True,
            "staged_results_written": 7,
        }

    monkeypatch.setattr(
        "services.ingestion.graph_backfill.backfill_failed_graph_chunks",
        fake_backfill_failed_graph_chunks,
    )

    result = await run_graph_promotion_jobs(
        db,
        qdrant_client=None,
        neo4j_driver=object(),
        corpus_id="corpus-1",
        user_id="user-1",
    )

    assert result["counts"]["partial"] == 1
    assert result["results"][0]["status"] == "partial"
    assert result["results"][0]["neo4j_write_latency_ms"] >= 0
    assert result["results"][0]["promoted_counts"] == {
        "ghost_b_rows_promoted": 7,
        "extraction_jobs_promoted": 6,
    }
    completion_update = jobs.update_one_calls[-1][1]["$set"]
    assert completion_update["status"] == "partial"
    assert completion_update["result"]["remaining_failed_chunks"] == 2
    assert completion_update["result"]["neo4j_write_latency_source"] == "graph_promotion_job"
    assert completion_update["neo4j_write_latency_ms"] >= 0
    assert completion_update["neo4j_write_latency_source"] == "graph_promotion_job"
    assert completion_update["promoted_counts"]["ghost_b_rows_promoted"] == 7


@pytest.mark.asyncio
async def test_run_graph_promotion_jobs_completion_is_guarded_by_claimed_owner(
    monkeypatch,
):
    jobs = _FakeQueuedJobsCollection(
        [
            {
                "job_id": "graph-job-1",
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "user_id": "user-1",
                "status": "queued",
            }
        ]
    )
    db = _FakeDb(
        {
            "graph_promotion_jobs": jobs,
            "ghost_b_extractions": _FakeUpdateCollection(modified_count=0),
            "extraction_jobs": _FakeUpdateCollection(modified_count=0),
        }
    )

    async def fake_backfill_failed_graph_chunks(**_kwargs):
        assert jobs.jobs[0]["status"] == "running"
        original_started = jobs.jobs[0]["started_at"]
        jobs.jobs[0]["started_at"] = original_started.replace(microsecond=1)
        jobs.jobs[0]["last_run_at"] = jobs.jobs[0]["started_at"]
        jobs.jobs[0]["lease_until"] = jobs.jobs[0]["lease_until"].replace(microsecond=1)
        return {
            "status": "done",
            "doc_id": "doc-1",
            "corpus_id": "corpus-1",
            "remaining_failed_chunks": 0,
            "neo4j_flushed": True,
            "staged_results_written": 3,
        }

    monkeypatch.setattr(
        "services.ingestion.graph_backfill.backfill_failed_graph_chunks",
        fake_backfill_failed_graph_chunks,
    )

    result = await run_graph_promotion_jobs(
        db,
        qdrant_client=None,
        neo4j_driver=object(),
        corpus_id="corpus-1",
        user_id="user-1",
    )

    assert result["counts"]["done"] == 0
    assert result["counts"]["lost_ownership"] == 1
    assert result["results"][0]["status"] == "lost_ownership"
    assert result["results"][0]["attempted_status"] == "done"
    assert jobs.jobs[0]["status"] == "running"


@pytest.mark.asyncio
async def test_run_graph_promotion_jobs_blocks_extraction_required_without_model_calls(monkeypatch):
    jobs = _FakeQueuedJobsCollection(
        [
            {
                "job_id": "graph-job-1",
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "user_id": "user-1",
                "status": "queued",
            }
        ]
    )
    db = _FakeDb(
        {
            "graph_promotion_jobs": jobs,
            "ghost_b_extractions": _FakeUpdateCollection(modified_count=0),
            "extraction_jobs": _FakeUpdateCollection(modified_count=0),
        }
    )

    async def fake_backfill_failed_graph_chunks(**kwargs):
        assert kwargs["allow_extraction"] is False
        return {
            "status": "blocked_extraction_required",
            "doc_id": "doc-1",
            "corpus_id": "corpus-1",
            "remaining_failed_chunks": 3,
            "neo4j_flushed": False,
            "extraction_retry_skipped": True,
        }

    monkeypatch.setattr(
        "services.ingestion.graph_backfill.backfill_failed_graph_chunks",
        fake_backfill_failed_graph_chunks,
    )

    result = await run_graph_promotion_jobs(
        db,
        qdrant_client=None,
        neo4j_driver=object(),
        corpus_id="corpus-1",
        user_id="user-1",
    )

    assert result["counts"]["blocked_no_extractions"] == 1
    assert result["counts"]["failed"] == 0
    assert result["results"][0]["status"] == "blocked_no_extractions"
    assert result["results"][0]["neo4j_write_latency_ms"] >= 0
    completion_update = jobs.update_one_calls[-1][1]["$set"]
    assert completion_update["status"] == "blocked_no_extractions"
    assert completion_update["result"]["extraction_retry_skipped"] is True
    assert completion_update["neo4j_write_latency_source"] == "graph_promotion_job"

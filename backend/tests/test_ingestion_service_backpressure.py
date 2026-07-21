import asyncio
from unittest.mock import AsyncMock

import pytest

from services.ingestion_service import IngestionService


class _FakeSummaryCostController:
    async def snapshot(self):
        return {"run_id": "test-summary-cost-run", "authorized_usd": "1.000000000"}


def _install_fake_summary_cost_controller(monkeypatch):
    open_controller = AsyncMock(return_value=_FakeSummaryCostController())
    monkeypatch.setattr(
        "services.ingestion.summary_cost_control.SummaryCostController.open",
        open_controller,
    )
    return open_controller


def _paused_result(lane_key: str, operation: str) -> dict:
    return {
        "corpus_id": "corpus-1",
        "status": "paused_pressure",
        "operation": operation,
        "reason": f"{lane_key}=false",
        "pressure": {"status": "high", "backpressure": {lane_key: False}},
        "readiness": {"corpus_id": "corpus-1", "pressure": {"status": "high"}},
    }


@pytest.mark.asyncio
async def test_direct_extraction_job_run_respects_backpressure(monkeypatch):
    service = IngestionService()

    async def fake_pause(**kwargs):
        return _paused_result(kwargs["lane_key"], kwargs["operation"])

    async def fail_run(*_args, **_kwargs):
        raise AssertionError("provider extraction jobs must not run under pressure")

    monkeypatch.setattr(service, "_backpressure_pause_result", fake_pause)
    monkeypatch.setattr("services.ingestion.extraction_jobs.run_extraction_jobs", fail_run)

    result = await service.run_extraction_jobs(
        corpus_id="corpus-1",
        user_id="user-1",
    )

    assert result["status"] == "paused_pressure"
    assert result["operation"] == "extraction_jobs.run"
    assert result["claimed"] == 0
    assert result["counts"] == {}


@pytest.mark.asyncio
async def test_direct_graph_promotion_run_respects_backpressure(monkeypatch):
    service = IngestionService()

    async def fake_pause(**kwargs):
        return _paused_result(kwargs["lane_key"], kwargs["operation"])

    async def fail_run(*_args, **_kwargs):
        raise AssertionError("graph promotion jobs must not run under pressure")

    monkeypatch.setattr(service, "_backpressure_pause_result", fake_pause)
    monkeypatch.setattr("services.ingestion.graph_promotion_jobs.run_graph_promotion_jobs", fail_run)

    result = await service.run_graph_promotion_jobs(
        corpus_id="corpus-1",
        user_id="user-1",
    )

    assert result["status"] == "paused_pressure"
    assert result["operation"] == "graph_promotion_jobs.run"
    assert result["counts"] == {}


class _FakeParentChunks:
    async def count_documents(self, query):
        if "summary" in str(query):
            return 2
        return 5


class _FakeDb:
    def __getitem__(self, name):
        assert name == "parent_chunks"
        return _FakeParentChunks()


class _FakeQdrant:
    async def count(self, *_args, **_kwargs):
        class _Count:
            count = 0

        return _Count()


class _SummaryCursor:
    def __init__(self, rows):
        self.rows = rows
        self._limit = len(rows)

    def limit(self, value):
        self._limit = value
        return self

    async def to_list(self, length=None):
        limit = self._limit if length is None else min(self._limit, length)
        return self.rows[:limit]

    def __aiter__(self):
        self._iter = iter(self.rows[: self._limit])
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _SummaryParentChunks:
    def __init__(self):
        self.writes = []
        self.rows = [
            {
                "parent_id": "parent-1",
                "doc_id": "doc-1",
                "corpus_id": "corpus-1",
                "source_tier": "tier_a",
                "text": "A useful parent passage about customer trust.",
            }
        ]

    async def count_documents(self, query):
        if "summary" in str(query):
            return 0
        return 1

    def find(self, *_args, **_kwargs):
        return _SummaryCursor(list(self.rows))

    async def bulk_write(self, writes, **_kwargs):
        self.writes.extend(writes)


class _SummaryDb:
    def __init__(self):
        self.parent_chunks = _SummaryParentChunks()
        self.chunks = _SummaryParentChunks()
        self.chunks.rows = [
            {
                "chunk_id": "chunk-1",
                "parent_id": "parent-1",
                "text": "A child passage about customer trust.",
            }
        ]

    def __getitem__(self, name):
        assert name in {"parent_chunks", "chunks"}
        return getattr(self, name)


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


class _FakeCorpora:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query=None, *_args, **_kwargs):
        rows = self.rows
        def _corpus_id_filter(value):
            if not isinstance(value, dict):
                return None
            if value.get("corpus_id"):
                return value["corpus_id"]
            for clause in value.get("$and") or []:
                found = _corpus_id_filter(clause)
                if found:
                    return found
            return None

        corpus_id = _corpus_id_filter(query or {})
        if corpus_id:
            rows = [row for row in rows if row.get("corpus_id") == corpus_id]
        return _Cursor(rows)


class _AutoRepairDb:
    def __init__(self, rows=None):
        self.corpora = _FakeCorpora(
            rows
            or [
                {
                    "corpus_id": "corpus-1",
                    "user_id": "user-1",
                    "name": "Corpus",
                }
            ]
        )

    def __getitem__(self, name):
        assert name == "corpora"
        return self.corpora


@pytest.mark.asyncio
async def test_summary_backfill_respects_backpressure(monkeypatch):
    service = IngestionService()
    service._db = _FakeDb()
    service._qdrant = _FakeQdrant()

    async def fake_corpus(_corpus_id):
        return {"corpus_id": "corpus-1", "user_id": "user-1", "default_ingestion_config": {}}

    async def fake_pause(**kwargs):
        return _paused_result(kwargs["lane_key"], kwargs["operation"])

    async def fail_summarize(*_args, **_kwargs):
        raise AssertionError("summary model calls must not run under pressure")

    monkeypatch.setattr(service, "_get_corpus_raw", fake_corpus)
    monkeypatch.setattr(service, "_backpressure_pause_result", fake_pause)
    monkeypatch.setattr("services.ghost_a.summarize_parents", fail_summarize)
    open_controller = _install_fake_summary_cost_controller(monkeypatch)

    result = await service.backfill_parent_summaries(
        "corpus-1",
        user_id="user-1",
        generate=True,
        index=True,
        limit=10,
        summary_cost_run_id="test-summary-cost-run",
        summary_cost_authority_usd="1.00",
    )

    assert result["status"] == "paused_pressure"
    assert result["operation"] == "summaries.backfill"
    assert result["generated"] == 0
    assert result["attempted"] == 0
    assert result["indexed"] == 0
    assert result["index_scope"] == "paused_pressure"
    assert result["before"]["retrieval_parent_count"] == 5
    assert result["before"]["body_parent_count"] == 5
    assert result["after"] == result["before"]
    open_controller.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_summary_backfill_generates_but_defers_indexing_under_qdrant_pressure(monkeypatch):
    from services.ghost_a import SummaryResult

    service = IngestionService()
    service._db = _SummaryDb()
    service._qdrant = _FakeQdrant()

    async def fake_corpus(_corpus_id):
        return {
            "corpus_id": "corpus-1",
            "user_id": "user-1",
            "default_ingestion_config": {
                "summary_models": [
                    {
                        "provider_preset": "test",
                        "model": "test/summary",
                        "max_concurrent": 1,
                    }
                ],
                "target_qdrant_collections": ["hrag"],
            },
        }

    async def fake_readiness(_corpus_id):
        return {
            "corpus_id": "corpus-1",
            "pressure": {
                "status": "high",
                "backpressure": {
                    "summary_generation_allowed": True,
                    "summary_indexing_allowed": False,
                    "summary_backfill_allowed": True,
                },
            },
        }

    async def fake_summarize(tasks, **_kwargs):
        return [
            SummaryResult(
                parent_id=task.parent_id,
                doc_id=task.doc_id,
                corpus_id=task.corpus_id,
                source_tier=task.source_tier,
                summary="Generated summary.",
                domain="business_strategy",
            )
            for task in tasks
        ]

    async def fail_embed(*_args, **_kwargs):
        raise AssertionError("summary indexing should not embed under qdrant pressure")

    async def fail_upsert(*_args, **_kwargs):
        raise AssertionError("summary indexing should not upsert under qdrant pressure")

    monkeypatch.setattr(service, "_get_corpus_raw", fake_corpus)
    monkeypatch.setattr(service, "_compute_corpus_readiness_safely", fake_readiness)
    monkeypatch.setattr("services.ghost_a.summarize_parents", fake_summarize)
    monkeypatch.setattr("services.embedder.embed_batch", fail_embed)
    monkeypatch.setattr("services.storage.qdrant_writer.upsert_summaries", fail_upsert)

    async def fake_materialize(_corpus_id):
        return None

    monkeypatch.setattr(service, "_materialize_corpus_readiness_safely", fake_materialize)
    open_controller = _install_fake_summary_cost_controller(monkeypatch)

    result = await service.backfill_parent_summaries(
        "corpus-1",
        user_id="user-1",
        generate=True,
        index=True,
        limit=1,
        summary_cost_run_id="test-summary-cost-run",
        summary_cost_authority_usd="1.00",
    )

    assert result["status"] == "degraded"
    assert result["generated"] == 1
    assert result["indexed"] == 0
    assert result["index_requested"] is True
    assert result["index_scope"] == "skipped"
    assert result["index_deferred_by_pressure"] is True
    assert service._db.parent_chunks.writes
    open_controller.assert_awaited_once()


@pytest.mark.asyncio
async def test_summary_jobs_generate_but_defer_indexing_under_qdrant_pressure(monkeypatch):
    service = IngestionService()
    service._db = object()
    calls = []

    async def fake_readiness(_corpus_id):
        return {
            "corpus_id": "corpus-1",
            "pressure": {
                "status": "high",
                "backpressure": {
                    "summary_generation_allowed": True,
                    "summary_indexing_allowed": False,
                    "summary_backfill_allowed": True,
                },
            },
        }

    async def fake_parent_backfill(*args, **kwargs):
        calls.append(kwargs)
        return {
            "status": "partial",
            "generated": kwargs["limit"],
            "indexed": 0,
            "index_scope": "skipped",
        }

    async def fake_summary_job_runner(*_args, **kwargs):
        parent_result = await kwargs["parent_runner"](limit=3, doc_ids=["doc-1"])
        return {
            "status": "complete",
            "corpus_id": kwargs["corpus_id"],
            "claimed": 3,
            "parent_claimed": 3,
            "document_claimed": 0,
            "counts": {"succeeded": 3},
            "runner_results": {"retrieval_parent_summary": parent_result},
            "jobs": [],
        }

    monkeypatch.setattr(service, "_compute_corpus_readiness_safely", fake_readiness)
    monkeypatch.setattr(service, "backfill_parent_summaries", fake_parent_backfill)
    monkeypatch.setattr("services.ingestion.summary_jobs.run_summary_jobs", fake_summary_job_runner)
    open_controller = _install_fake_summary_cost_controller(monkeypatch)

    result = await service.run_summary_jobs(
        corpus_id="corpus-1",
        user_id="user-1",
        limit=3,
        summary_cost_run_id="test-summary-cost-run",
        summary_cost_authority_usd="1.00",
    )

    assert result["status"] == "complete"
    assert calls[0]["generate"] is True
    assert calls[0]["index"] is False
    parent_result = result["runner_results"]["retrieval_parent_summary"]
    assert parent_result["generated"] == 3
    assert parent_result["indexed"] == 0
    assert parent_result["index_scope"] == "paused_qdrant_pressure"
    assert parent_result["index_deferred_by_pressure"] is True
    open_controller.assert_awaited_once()


@pytest.mark.asyncio
async def test_auto_corpus_repair_tick_plans_without_running_provider_lanes(monkeypatch):
    service = IngestionService()
    service._db = _AutoRepairDb()
    calls = []

    class _Settings:
        INGEST_AUTO_REPAIR_ENABLED = True
        INGEST_AUTO_REPAIR_CORPUS_LIMIT = 5
        INGEST_AUTO_REPAIR_RUN_SOURCE_PARSE = False
        INGEST_AUTO_REPAIR_RUN_DOCUMENT_PIPELINE = False
        INGEST_AUTO_REPAIR_RUN_EXTRACTION = False
        INGEST_AUTO_REPAIR_RUN_SUMMARIES = False
        INGEST_AUTO_REPAIR_RUN_GRAPH = False

    async def fake_cycle(**kwargs):
        calls.append(kwargs)
        return {
            "status": "complete",
            "summary": {
                "readiness_status": "needs_repair",
                "queryable_docs": 2,
                "total_docs": 3,
                "failed_chunks": 1,
                "graph_jobs_queued": 4,
                "extraction_jobs_queued": 5,
                "summary_jobs_queued": 6,
                "document_pipeline_jobs_queued": 7,
            },
        }

    monkeypatch.setattr("services.ingestion_service.get_settings", lambda: _Settings())
    monkeypatch.setattr(service, "run_bounded_corpus_repair_cycle", fake_cycle)

    result = await service.run_auto_corpus_repair_tick()

    assert result["status"] == "complete"
    assert result["scanned"] == 1
    assert result["changed"] == 1
    assert calls[0]["apply"] is True
    assert calls[0]["reconcile_failures"] is True
    assert calls[0]["plan_extraction_jobs"] is True
    assert calls[0]["plan_summary_jobs"] is True
    assert calls[0]["plan_graph_jobs"] is True
    assert calls[0]["run_extraction_jobs"] is False
    assert calls[0]["run_summary_jobs"] is False
    assert calls[0]["run_graph_jobs"] is False
    assert calls[0]["run_document_pipeline_jobs"] is False


@pytest.mark.asyncio
async def test_auto_corpus_repair_tick_can_run_bounded_graph_lane(monkeypatch):
    service = IngestionService()
    service._db = _AutoRepairDb()
    calls = []

    class _Settings:
        INGEST_AUTO_REPAIR_ENABLED = True
        INGEST_AUTO_REPAIR_CORPUS_LIMIT = 5
        INGEST_AUTO_REPAIR_RUN_SOURCE_PARSE = False
        INGEST_AUTO_REPAIR_RUN_DOCUMENT_PIPELINE = False
        INGEST_AUTO_REPAIR_RUN_EXTRACTION = False
        INGEST_AUTO_REPAIR_RUN_SUMMARIES = False
        INGEST_AUTO_REPAIR_RUN_GRAPH = True
        INGEST_AUTO_REPAIR_GRAPH_RUN_LIMIT = 7

    async def fake_cycle(**kwargs):
        calls.append(kwargs)
        return {
            "status": "complete",
            "summary": {
                "readiness_status": "graph_pending",
                "queryable_docs": 10,
                "total_docs": 10,
                "failed_chunks": 0,
                "graph_jobs_queued": 3,
                "extraction_jobs_queued": 0,
                "summary_jobs_queued": 0,
                "document_pipeline_jobs_queued": 0,
            },
        }

    async def fake_run_graph_jobs(**kwargs):
        calls.append(("run_graph", kwargs))
        return {"status": "complete", "claimed": 2, "counts": {"done": 2}}

    monkeypatch.setattr("services.ingestion_service.get_settings", lambda: _Settings())
    monkeypatch.setattr(service, "run_bounded_corpus_repair_cycle", fake_cycle)
    monkeypatch.setattr(service, "run_graph_promotion_jobs", fake_run_graph_jobs)

    result = await service.run_auto_corpus_repair_tick()

    assert result["status"] == "complete"
    assert calls[0]["run_extraction_jobs"] is False
    assert calls[0]["run_summary_jobs"] is False
    assert calls[0]["run_document_pipeline_jobs"] is False
    assert calls[0]["run_graph_jobs"] is False
    assert calls[0]["graph_run_limit"] == 7
    assert calls[1] == (
        "run_graph",
        {"corpus_id": "corpus-1", "user_id": "user-1", "limit": 7},
    )
    assert result["corpora"][0]["provider_lanes"]["graph_promotion"] == {
        "status": "complete",
        "claimed": 2,
        "counts": {"done": 2},
    }


@pytest.mark.asyncio
async def test_auto_corpus_repair_tick_runs_source_parse_lane(monkeypatch):
    service = IngestionService()
    service._db = _AutoRepairDb()
    calls = []

    class _Settings:
        INGEST_AUTO_REPAIR_ENABLED = True
        INGEST_AUTO_REPAIR_CORPUS_LIMIT = 5
        INGEST_AUTO_REPAIR_RUN_SOURCE_PARSE = True
        INGEST_AUTO_REPAIR_SOURCE_PARSE_RUN_LIMIT = 11
        INGEST_AUTO_REPAIR_RUN_DOCUMENT_PIPELINE = False
        INGEST_AUTO_REPAIR_RUN_EXTRACTION = False
        INGEST_AUTO_REPAIR_RUN_SUMMARIES = False
        INGEST_AUTO_REPAIR_RUN_GRAPH = False

    async def fake_cycle(**kwargs):
        calls.append(("cycle", kwargs))
        return {
            "status": "complete",
            "summary": {
                "readiness_status": "needs_source_parse",
                "queryable_docs": 0,
                "total_docs": 1,
                "failed_chunks": 0,
                "graph_jobs_queued": 0,
                "extraction_jobs_queued": 0,
                "summary_jobs_queued": 0,
                "document_pipeline_jobs_queued": 0,
            },
        }

    async def fake_run_source_parse_jobs(**kwargs):
        calls.append(("run_source", kwargs))
        return {
            "status": "started",
            "claimed": 1,
            "requested": 1,
            "eligible_items": 1,
            "counts": {"running": 1},
        }

    monkeypatch.setattr("services.ingestion_service.get_settings", lambda: _Settings())
    monkeypatch.setattr(service, "run_bounded_corpus_repair_cycle", fake_cycle)
    monkeypatch.setattr(service, "run_source_parse_jobs", fake_run_source_parse_jobs)

    result = await service.run_auto_corpus_repair_tick()

    assert calls[0][0] == "cycle"
    assert calls[0][1]["run_source_parse_jobs"] is False
    assert calls[1] == (
        "run_source",
        {"corpus_id": "corpus-1", "user_id": "user-1", "limit": 11},
    )
    assert result["corpora"][0]["provider_lanes"]["source_parse"] == {
        "status": "started",
        "claimed": 1,
        "counts": {"running": 1},
    }


@pytest.mark.asyncio
async def test_auto_corpus_repair_tick_runs_provider_lanes_concurrently(monkeypatch):
    service = IngestionService()
    service._db = _AutoRepairDb()
    calls = []
    active = 0
    peak = 0

    class _Settings:
        INGEST_AUTO_REPAIR_ENABLED = True
        INGEST_AUTO_REPAIR_CORPUS_LIMIT = 5
        INGEST_AUTO_REPAIR_RUN_SOURCE_PARSE = False
        INGEST_AUTO_REPAIR_RUN_DOCUMENT_PIPELINE = False
        INGEST_AUTO_REPAIR_RUN_EXTRACTION = True
        INGEST_AUTO_REPAIR_EXTRACTION_RUN_LIMIT = 41
        INGEST_AUTO_REPAIR_RUN_SUMMARIES = True
        INGEST_AUTO_REPAIR_SUMMARY_RUN_LIMIT = 43
        INGEST_AUTO_REPAIR_RUN_GRAPH = False

    async def fake_cycle(**kwargs):
        calls.append(kwargs)
        return {
            "status": "complete",
            "summary": {
                "readiness_status": "needs_repair",
                "queryable_docs": 1,
                "total_docs": 1,
                "failed_chunks": 0,
                "graph_jobs_queued": 0,
                "extraction_jobs_queued": 1,
                "summary_jobs_queued": 1,
                "document_pipeline_jobs_queued": 0,
            },
        }

    async def _lane_result(name, claimed):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"status": "complete", "claimed": claimed, "counts": {"succeeded": claimed}}

    async def fake_run_summary_jobs(**kwargs):
        assert kwargs["limit"] == 43
        return await _lane_result("summary", 3)

    async def fake_run_extraction_jobs(**kwargs):
        assert kwargs["limit"] == 41
        return await _lane_result("extraction", 5)

    monkeypatch.setattr("services.ingestion_service.get_settings", lambda: _Settings())
    monkeypatch.setattr(service, "run_bounded_corpus_repair_cycle", fake_cycle)
    monkeypatch.setattr(service, "run_summary_jobs", fake_run_summary_jobs)
    monkeypatch.setattr(service, "run_extraction_jobs", fake_run_extraction_jobs)

    result = await service.run_auto_corpus_repair_tick()

    assert peak == 2
    assert calls[0]["run_document_pipeline_jobs"] is False
    assert calls[0]["run_graph_jobs"] is False
    assert calls[0]["run_summary_jobs"] is False
    assert calls[0]["run_extraction_jobs"] is False
    assert result["changed"] == 1
    assert result["corpora"][0]["provider_lanes"] == {
        "summary": {"status": "complete", "claimed": 3, "counts": {"succeeded": 3}},
        "extraction": {"status": "complete", "claimed": 5, "counts": {"succeeded": 5}},
    }


@pytest.mark.asyncio
async def test_auto_corpus_repair_tick_does_not_serialize_corpora(monkeypatch):
    service = IngestionService()
    service._db = _AutoRepairDb(
        [
            {"corpus_id": "local-corpus", "user_id": "user-1", "name": "Local"},
            {"corpus_id": "cloud-corpus", "user_id": "user-1", "name": "Cloud"},
        ]
    )
    cloud_summary_started = asyncio.Event()

    class _Settings:
        INGEST_AUTO_REPAIR_ENABLED = True
        INGEST_AUTO_REPAIR_CORPUS_LIMIT = 2
        INGEST_AUTO_REPAIR_CORPUS_CONCURRENCY = 2
        INGEST_AUTO_REPAIR_RUN_SOURCE_PARSE = False
        INGEST_AUTO_REPAIR_RUN_DOCUMENT_PIPELINE = True
        INGEST_AUTO_REPAIR_DOCUMENT_RUN_LIMIT = 1
        INGEST_AUTO_REPAIR_RUN_EXTRACTION = False
        INGEST_AUTO_REPAIR_RUN_SUMMARIES = True
        INGEST_AUTO_REPAIR_SUMMARY_RUN_LIMIT = 1
        INGEST_AUTO_REPAIR_RUN_GRAPH = False

    async def fake_cycle(**_kwargs):
        return {
            "status": "complete",
            "summary": {
                "readiness_status": "needs_repair",
                "queryable_docs": 1,
                "total_docs": 1,
                "failed_chunks": 0,
                "graph_jobs_queued": 0,
                "extraction_jobs_queued": 0,
                "summary_jobs_queued": 1,
                "document_pipeline_jobs_queued": 1,
            },
        }

    async def fake_run_document_jobs(**kwargs):
        if kwargs["corpus_id"] == "local-corpus":
            await asyncio.wait_for(cloud_summary_started.wait(), timeout=0.5)
        return {"status": "complete", "claimed": 1, "counts": {"succeeded": 1}}

    async def fake_run_summary_jobs(**kwargs):
        if kwargs["corpus_id"] == "cloud-corpus":
            cloud_summary_started.set()
        return {"status": "complete", "claimed": 1, "counts": {"succeeded": 1}}

    async def fake_snapshot(_db, corpus_id):
        return {"corpus_id": corpus_id, "total_gap_count": 2}

    async def fake_state(_db, _corpus_id):
        return {}

    async def fake_record(*_args, **_kwargs):
        return None

    monkeypatch.setattr("services.ingestion_service.get_settings", lambda: _Settings())
    monkeypatch.setattr(service, "run_bounded_corpus_repair_cycle", fake_cycle)
    monkeypatch.setattr(service, "run_document_pipeline_jobs", fake_run_document_jobs)
    monkeypatch.setattr(service, "run_summary_jobs", fake_run_summary_jobs)
    monkeypatch.setattr("services.ingestion.repair_scheduler.quick_repair_gap_snapshot", fake_snapshot)
    monkeypatch.setattr("services.ingestion.repair_scheduler.load_scheduler_state", fake_state)
    monkeypatch.setattr(
        "services.ingestion.repair_scheduler.backoff_decision",
        lambda **_kwargs: {"should_run": True, "reason": "gaps_present"},
    )
    monkeypatch.setattr("services.ingestion.repair_scheduler.record_scheduler_outcome", fake_record)

    result = await service.run_auto_corpus_repair_tick()

    assert result["status"] == "complete"
    assert result["scanned"] == 2
    assert result["corpus_concurrency"] == 2
    assert cloud_summary_started.is_set()

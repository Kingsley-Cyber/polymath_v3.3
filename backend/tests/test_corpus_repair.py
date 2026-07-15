import pytest

from services.ingestion import corpus_repair
from services.ingestion.corpus_repair import build_repair_cycle_summary


class _RepairRuns:
    def __init__(self):
        self.rows = []

    async def insert_one(self, row):
        self.rows.append(row)


class _FakeDb:
    def __init__(self):
        self.ingest_repair_runs = _RepairRuns()

    def __getitem__(self, name):
        return getattr(self, name)


def test_repair_cycle_summary_surfaces_remaining_work():
    summary = build_repair_cycle_summary(
        steps=[
            {"name": "failure_reconciliation", "status": "planned", "changed": True},
            {
                "name": "ghost_b_stage_identity_backfill",
                "status": "planned",
                "changed": True,
                "result": {"planned": 12, "modified": 0},
            },
            {
                "name": "promoted_extraction_mark_backfill",
                "status": "planned",
                "changed": True,
                "result": {"planned_rows": 9, "modified_rows": 0},
            },
            {"name": "graph_promotion_plan", "status": "planned", "changed": True},
            {"name": "graph_promotion_run", "status": "skipped_dry_run", "changed": False},
        ],
        readiness={
            "status": "needs_repair",
            "documents": {"queryable": 10, "total": 12},
            "summaries": {"body_parent_missing": 4, "document_missing": 2},
            "graph": {"pending": 3, "failed_chunks": 7, "stale_failure_rows": 1},
            "repair": {
                "graph_promotion_jobs": {
                    "queued": 2,
                    "running": 1,
                    "blocked_failed_chunks": 5,
                    "blocked_no_extractions": 1,
                }
            },
        },
    )

    assert summary["readiness_status"] == "needs_repair"
    assert summary["queryable_docs"] == 10
    assert summary["total_docs"] == 12
    assert summary["main_summary_missing"] == 4
    assert summary["document_summary_missing"] == 2
    assert summary["graph_pending"] == 3
    assert summary["failed_chunks"] == 7
    assert summary["stale_failure_rows"] == 1
    assert summary["ghost_b_stage_identity_planned"] == 12
    assert summary["ghost_b_stage_identity_backfilled"] == 0
    assert summary["promoted_extraction_marks_planned"] == 9
    assert summary["promoted_extraction_marks_backfilled"] == 0
    assert summary["graph_jobs_queued"] == 2
    assert summary["graph_jobs_running"] == 1
    assert summary["graph_jobs_blocked"] == 6
    assert summary["steps"] == [
        {"name": "failure_reconciliation", "status": "planned", "changed": True},
        {"name": "ghost_b_stage_identity_backfill", "status": "planned", "changed": True},
        {"name": "promoted_extraction_mark_backfill", "status": "planned", "changed": True},
        {"name": "graph_promotion_plan", "status": "planned", "changed": True},
        {"name": "graph_promotion_run", "status": "skipped_dry_run", "changed": False},
    ]


def test_repair_cycle_summary_handles_missing_readiness():
    summary = build_repair_cycle_summary(steps=[], readiness=None)

    assert summary["readiness_status"] == "unknown"
    assert summary["queryable_docs"] == 0
    assert summary["graph_jobs_blocked"] == 0
    assert summary["steps"] == []


def test_repair_cycle_summary_uses_dry_run_plan_counts_before_jobs_exist():
    summary = build_repair_cycle_summary(
        steps=[
            {
                "name": "graph_promotion_plan",
                "status": "planned",
                "changed": True,
                "result": {
                    "counts": {
                        "queued": 3,
                        "blocked_failed_chunks": 14,
                        "blocked_no_extractions": 2,
                    }
                },
            },
            {
                "name": "extraction_job_plan",
                "status": "planned",
                "changed": True,
                "result": {
                    "counts": {
                        "queued": 11,
                        "provider_failed": 3,
                        "validation_failed": 2,
                        "blocked_provider_contract": 4,
                    }
                },
            },
            {
                "name": "source_parse_job_plan",
                "status": "planned",
                "changed": True,
                "result": {
                    "counts": {
                        "queued": 4,
                        "blocked_source_missing": 1,
                    }
                },
            },
            {
                "name": "document_pipeline_job_plan",
                "status": "planned",
                "changed": True,
                "result": {
                    "counts": {
                        "queued": 5,
                        "blocked_no_source": 1,
                        "blocked_mongo_state": 2,
                    }
                },
            },
            {
                "name": "summary_job_plan",
                "status": "planned",
                "changed": True,
                "result": {
                    "counts": {
                        "queued": 7,
                        "blocked_no_parent_summaries": 2,
                        "blocked_parent_summaries_incomplete": 1,
                    }
                },
            },
        ],
        readiness={
            "status": "graph_pending",
            "documents": {"queryable": 17, "total": 20},
            "summaries": {"body_parent_missing": 0, "document_missing": 0},
            "graph": {"pending": 17, "failed_chunks": 0, "stale_failure_rows": 0},
            "repair": {
                "graph_promotion_jobs": {},
                "source_parse_jobs": {},
                "document_pipeline_jobs": {},
                "extraction_jobs": {},
            },
        },
    )

    assert summary["graph_jobs_queued"] == 3
    assert summary["graph_jobs_blocked"] == 16
    assert summary["extraction_jobs_queued"] == 11
    assert summary["extraction_jobs_failed"] == 5
    assert summary["extraction_jobs_blocked"] == 4
    assert summary["source_parse_jobs_queued"] == 4
    assert summary["source_parse_jobs_blocked"] == 1
    assert summary["document_pipeline_jobs_queued"] == 5
    assert summary["document_pipeline_jobs_blocked"] == 3
    assert summary["summary_jobs_queued"] == 7
    assert summary["summary_jobs_pending"] == 10
    assert summary["summary_jobs_waiting_dependencies"] == 3
    assert summary["summary_jobs_blocked"] == 0


def test_repair_cycle_summary_surfaces_backpressure_blocked_steps():
    summary = build_repair_cycle_summary(
        steps=[
            {"name": "extraction_job_run", "status": "skipped_pressure", "changed": False},
            {"name": "graph_promotion_run", "status": "skipped_pressure", "changed": False},
        ],
        readiness={
            "status": "needs_repair",
            "documents": {"queryable": 1, "total": 1},
            "summaries": {"body_parent_missing": 0, "document_missing": 0},
            "graph": {"pending": 1, "failed_chunks": 1, "stale_failure_rows": 0},
            "repair": {},
            "pressure": {"status": "high"},
        },
    )

    assert summary["pressure_status"] == "high"
    assert summary["backpressure_blocked_steps"] == [
        "extraction_job_run",
        "graph_promotion_run",
    ]


def test_repair_cycle_summary_surfaces_built_document_summaries():
    summary = build_repair_cycle_summary(
        steps=[
            {
                "name": "document_summary_backfill",
                "status": "complete",
                "changed": True,
                "result": {"built": 6},
            }
        ],
        readiness={
            "status": "summaries_pending",
            "documents": {"queryable": 10, "total": 10},
            "summaries": {"body_parent_missing": 0, "document_missing": 4},
            "graph": {"pending": 0, "failed_chunks": 0, "stale_failure_rows": 0},
            "repair": {},
        },
    )

    assert summary["document_summaries_built"] == 6
    assert summary["document_summary_missing"] == 4


def test_repair_cycle_summary_prefers_post_extraction_graph_replan():
    summary = build_repair_cycle_summary(
        steps=[
            {
                "name": "graph_promotion_plan",
                "status": "complete",
                "changed": True,
                "result": {
                    "counts": {
                        "blocked_failed_chunks": 8,
                    }
                },
            },
            {
                "name": "extraction_job_run",
                "status": "complete",
                "changed": True,
                "result": {
                    "claimed": 8,
                    "counts": {"succeeded": 8},
                },
            },
            {
                "name": "graph_promotion_replan_after_extraction",
                "status": "complete",
                "changed": True,
                "result": {
                    "counts": {
                        "queued": 8,
                    }
                },
            },
        ],
        readiness={
            "status": "graph_pending",
            "documents": {"queryable": 8, "total": 8},
            "summaries": {"body_parent_missing": 0, "document_missing": 0},
            "graph": {"pending": 8, "failed_chunks": 0, "stale_failure_rows": 0},
            "repair": {
                "graph_promotion_jobs": {},
                "source_parse_jobs": {},
                "document_pipeline_jobs": {},
                "extraction_jobs": {},
            },
        },
    )

    assert summary["graph_jobs_queued"] == 8
    assert summary["graph_jobs_blocked"] == 0


@pytest.mark.asyncio
async def test_repair_cycle_replans_graph_after_extraction_run(monkeypatch):
    calls = []

    async def fake_readiness(_db, _corpus_id):
        return {
            "status": "graph_pending",
            "documents": {"queryable": 1, "total": 1},
            "summaries": {"body_parent_missing": 0, "document_missing": 0},
            "graph": {"pending": 1, "failed_chunks": 0, "stale_failure_rows": 0},
            "repair": {"graph_promotion_jobs": {}, "source_parse_jobs": {}, "extraction_jobs": {}},
        }

    async def fake_materialize(_db, _corpus_id):
        calls.append("materialize")
        return {
            "status": "graph_pending",
            "documents": {"queryable": 1, "total": 1},
            "summaries": {"body_parent_missing": 0, "document_missing": 0},
            "graph": {"pending": 1, "failed_chunks": 0, "stale_failure_rows": 0},
            "repair": {
                "graph_promotion_jobs": {"queued": 1},
                "source_parse_jobs": {},
                "document_pipeline_jobs": {},
                "extraction_jobs": {},
            },
            "schema_version": "corpus_readiness.v1",
        }

    async def fake_reconcile(*_args, **_kwargs):
        calls.append("reconcile")
        return {"status": "complete"}

    async def fake_plan_source_parse(*_args, **_kwargs):
        calls.append("plan_source_parse")
        return {"status": "complete", "planned": 1, "counts": {"queued": 1}}

    async def fake_plan_document_pipeline(*_args, **_kwargs):
        calls.append("plan_document_pipeline")
        return {"status": "complete", "planned": 1, "counts": {"queued": 1}}

    async def fake_plan_graph(*_args, **_kwargs):
        calls.append("plan_graph")
        return {"status": "complete", "planned": 1, "counts": {"queued": 1}}

    async def fake_plan_extraction(*_args, **_kwargs):
        calls.append("plan_extraction")
        return {"status": "complete", "planned": 1, "counts": {"queued": 1}}

    async def fake_plan_summary(*_args, **_kwargs):
        calls.append("plan_summary")
        return {"status": "complete", "planned": 1, "counts": {"queued": 1}}

    async def fake_run_extraction(*_args, **_kwargs):
        calls.append("run_extraction")
        return {"status": "complete", "claimed": 1, "counts": {"succeeded": 1}}

    async def fake_run_graph(*_args, **_kwargs):
        calls.append("run_graph")
        return {"status": "complete", "counts": {"done": 1}}

    monkeypatch.setattr(corpus_repair, "compute_corpus_readiness", fake_readiness)
    monkeypatch.setattr(corpus_repair, "materialize_corpus_readiness", fake_materialize)
    monkeypatch.setattr(corpus_repair, "reconcile_ghost_b_failure_metadata", fake_reconcile)
    monkeypatch.setattr(corpus_repair, "plan_source_parse_jobs", fake_plan_source_parse)
    monkeypatch.setattr(corpus_repair, "plan_document_pipeline_jobs", fake_plan_document_pipeline)
    monkeypatch.setattr(corpus_repair, "plan_graph_promotion_jobs", fake_plan_graph)
    monkeypatch.setattr(corpus_repair, "plan_extraction_jobs", fake_plan_extraction)
    monkeypatch.setattr(corpus_repair, "plan_summary_jobs", fake_plan_summary)
    monkeypatch.setattr(corpus_repair, "run_extraction_jobs", fake_run_extraction)
    monkeypatch.setattr(corpus_repair, "run_graph_promotion_jobs", fake_run_graph)

    result = await corpus_repair.run_bounded_corpus_repair_cycle(
        _FakeDb(),
        corpus_id="corpus-1",
        user_id="user-1",
        qdrant_client=object(),
        neo4j_driver=object(),
        apply=True,
        run_extraction_job_rows=True,
        run_graph_jobs=True,
    )

    assert calls == [
        "reconcile",
        "plan_source_parse",
        "plan_document_pipeline",
        "plan_graph",
        "plan_extraction",
        "plan_summary",
        "run_extraction",
        "plan_graph",
        "run_graph",
        "materialize",
    ]
    assert [
        step["name"] for step in result["steps"]
    ] == [
        "failure_reconciliation",
        "source_parse_job_plan",
        "document_pipeline_job_plan",
        "graph_promotion_plan",
        "extraction_job_plan",
        "summary_job_plan",
        "extraction_job_run",
        "graph_promotion_replan_after_extraction",
        "graph_promotion_run",
    ]
    assert result["readiness_after"]["schema_version"] == "corpus_readiness.v1"


@pytest.mark.asyncio
async def test_repair_cycle_runs_document_summary_backfill(monkeypatch):
    calls = []

    async def fake_readiness(_db, _corpus_id):
        return {
            "status": "summaries_pending",
            "documents": {"queryable": 2, "total": 2},
            "summaries": {"body_parent_missing": 0, "document_missing": 2},
            "graph": {"pending": 0, "failed_chunks": 0, "stale_failure_rows": 0},
            "repair": {"graph_promotion_jobs": {}, "source_parse_jobs": {}, "extraction_jobs": {}},
            "pressure": {"status": "normal", "backpressure": {"summary_backfill_allowed": True}},
        }

    async def fake_materialize(_db, _corpus_id):
        calls.append("materialize")
        return {
            "status": "fully_enriched",
            "documents": {"queryable": 2, "total": 2},
            "summaries": {"body_parent_missing": 0, "document_missing": 0},
            "graph": {"pending": 0, "failed_chunks": 0, "stale_failure_rows": 0},
            "repair": {"graph_promotion_jobs": {}, "source_parse_jobs": {}, "extraction_jobs": {}},
            "schema_version": "corpus_readiness.v1",
        }

    class _SummaryService:
        async def backfill_document_summaries(self, **kwargs):
            calls.append(("doc_backfill", kwargs["limit"]))
            assert kwargs["summary_cost_run_id"] == "repair-cost-run"
            assert kwargs["summary_cost_authority_usd"] == "1.00"
            return {
                "status": "complete",
                "attempted": 2,
                "built": 2,
                "skipped": 0,
                "failed": 0,
            }

    async def fake_plan_summary(*_args, **_kwargs):
        calls.append("plan_summary")
        return {"status": "complete", "planned": 2, "counts": {"queued": 2}}

    monkeypatch.setattr(corpus_repair, "compute_corpus_readiness", fake_readiness)
    monkeypatch.setattr(corpus_repair, "materialize_corpus_readiness", fake_materialize)
    monkeypatch.setattr(corpus_repair, "plan_summary_jobs", fake_plan_summary)

    result = await corpus_repair.run_bounded_corpus_repair_cycle(
        _FakeDb(),
        corpus_id="corpus-1",
        user_id="user-1",
        ingestion_service=_SummaryService(),
        apply=True,
        reconcile_failures=False,
        plan_source_parse_job_rows=False,
        plan_document_pipeline_job_rows=False,
        plan_graph_jobs=False,
        plan_extraction_job_rows=False,
        run_document_summaries=True,
        document_summary_limit=7,
        summary_cost_run_id="repair-cost-run",
        summary_cost_authority_usd="1.00",
        record_run=False,
    )

    assert calls == ["plan_summary", ("doc_backfill", 7), "materialize"]
    assert [step["name"] for step in result["steps"]] == [
        "summary_job_plan",
        "document_summary_backfill",
    ]
    assert result["steps"][1]["status"] == "complete"
    assert result["summary"]["document_summaries_built"] == 2


@pytest.mark.asyncio
async def test_repair_cycle_replans_document_summaries_after_parent_summary_run(monkeypatch):
    calls = []

    async def fake_readiness(_db, _corpus_id):
        return {
            "status": "summaries_pending",
            "documents": {"queryable": 1, "total": 1},
            "summaries": {"body_parent_missing": 1, "document_missing": 1},
            "graph": {"pending": 0, "failed_chunks": 0, "stale_failure_rows": 0},
            "repair": {"summary_jobs": {"queued": 1}},
            "pressure": {"status": "normal", "backpressure": {"summary_backfill_allowed": True}},
        }

    async def fake_materialize(_db, _corpus_id):
        calls.append("materialize")
        return {
            "status": "fully_enriched",
            "documents": {"queryable": 1, "total": 1},
            "summaries": {"body_parent_missing": 0, "document_missing": 0},
            "graph": {"pending": 0, "failed_chunks": 0, "stale_failure_rows": 0},
            "repair": {"summary_jobs": {}},
            "schema_version": "corpus_readiness.v1",
        }

    async def fake_plan_summary(*_args, **kwargs):
        calls.append(("plan_summary", kwargs.get("kinds")))
        return {"status": "complete", "planned": 1, "counts": {"queued": 1}}

    class _SummaryService:
        async def run_summary_jobs(self, **kwargs):
            calls.append(("run_summary", kwargs.get("kinds")))
            if kwargs.get("kinds") == ["document_summary"]:
                return {
                    "status": "complete",
                    "claimed": 1,
                    "parent_claimed": 0,
                    "document_claimed": 1,
                    "counts": {"succeeded": 1},
                }
            return {
                "status": "complete",
                "claimed": 1,
                "parent_claimed": 1,
                "document_claimed": 0,
                "counts": {"succeeded": 1},
            }

    monkeypatch.setattr(corpus_repair, "compute_corpus_readiness", fake_readiness)
    monkeypatch.setattr(corpus_repair, "materialize_corpus_readiness", fake_materialize)
    monkeypatch.setattr(corpus_repair, "plan_summary_jobs", fake_plan_summary)

    result = await corpus_repair.run_bounded_corpus_repair_cycle(
        _FakeDb(),
        corpus_id="corpus-1",
        user_id="user-1",
        ingestion_service=_SummaryService(),
        apply=True,
        reconcile_failures=False,
        plan_source_parse_job_rows=False,
        plan_document_pipeline_job_rows=False,
        plan_graph_jobs=False,
        plan_extraction_job_rows=False,
        plan_summary_job_rows=True,
        run_summary_job_rows=True,
        record_run=False,
    )

    assert calls == [
        ("plan_summary", None),
        ("run_summary", None),
        ("plan_summary", ["document_summary"]),
        ("run_summary", ["document_summary"]),
        "materialize",
    ]
    assert [step["name"] for step in result["steps"]] == [
        "summary_job_plan",
        "summary_job_run",
        "document_summary_replan_after_parent_summary",
        "document_summary_run_after_parent_summary",
    ]
    assert result["steps"][2]["changed"] is True
    assert result["steps"][3]["result"]["document_claimed"] == 1
    assert result["summary"]["summary_jobs_queued"] == 1


@pytest.mark.asyncio
async def test_repair_cycle_backpressure_skips_heavy_execution(monkeypatch):
    calls = []

    high_pressure = {
        "status": "needs_repair",
        "documents": {"queryable": 1, "total": 1},
        "summaries": {"body_parent_missing": 0, "document_missing": 0},
        "graph": {"pending": 1, "failed_chunks": 1, "stale_failure_rows": 0},
        "repair": {
            "graph_promotion_jobs": {"queued": 1},
            "source_parse_jobs": {"queued": 1},
            "document_pipeline_jobs": {"queued": 1},
            "extraction_jobs": {"queued": 1},
        },
        "pressure": {
            "status": "high",
            "reasons": ["backend_rss_over_soft_limit"],
            "recommendations": ["pause_nonessential_backfills"],
            "backpressure": {
                "extraction_backfill_allowed": False,
                "graph_promotion_allowed": False,
            },
        },
    }

    async def fake_readiness(_db, _corpus_id):
        return high_pressure

    async def fake_materialize(_db, _corpus_id):
        calls.append("materialize")
        return {**high_pressure, "schema_version": "corpus_readiness.v1"}

    async def fake_reconcile(*_args, **_kwargs):
        calls.append("reconcile")
        return {"status": "complete"}

    async def fake_plan_source_parse(*_args, **_kwargs):
        calls.append("plan_source_parse")
        return {"status": "complete", "planned": 1, "counts": {"queued": 1}}

    async def fake_plan_document_pipeline(*_args, **_kwargs):
        calls.append("plan_document_pipeline")
        return {"status": "complete", "planned": 1, "counts": {"queued": 1}}

    async def fake_plan_graph(*_args, **_kwargs):
        calls.append("plan_graph")
        return {"status": "complete", "planned": 1, "counts": {"queued": 1}}

    async def fake_plan_extraction(*_args, **_kwargs):
        calls.append("plan_extraction")
        return {"status": "complete", "planned": 1, "counts": {"queued": 1}}

    async def fake_plan_summary(*_args, **_kwargs):
        calls.append("plan_summary")
        return {"status": "complete", "planned": 1, "counts": {"queued": 1}}

    async def fail_run_extraction(*_args, **_kwargs):
        raise AssertionError("extraction run must be pressure-gated")

    async def fail_run_graph(*_args, **_kwargs):
        raise AssertionError("graph run must be pressure-gated")

    monkeypatch.setattr(corpus_repair, "compute_corpus_readiness", fake_readiness)
    monkeypatch.setattr(corpus_repair, "materialize_corpus_readiness", fake_materialize)
    monkeypatch.setattr(corpus_repair, "reconcile_ghost_b_failure_metadata", fake_reconcile)
    monkeypatch.setattr(corpus_repair, "plan_source_parse_jobs", fake_plan_source_parse)
    monkeypatch.setattr(corpus_repair, "plan_document_pipeline_jobs", fake_plan_document_pipeline)
    monkeypatch.setattr(corpus_repair, "plan_graph_promotion_jobs", fake_plan_graph)
    monkeypatch.setattr(corpus_repair, "plan_extraction_jobs", fake_plan_extraction)
    monkeypatch.setattr(corpus_repair, "plan_summary_jobs", fake_plan_summary)
    monkeypatch.setattr(corpus_repair, "run_extraction_jobs", fail_run_extraction)
    monkeypatch.setattr(corpus_repair, "run_graph_promotion_jobs", fail_run_graph)

    result = await corpus_repair.run_bounded_corpus_repair_cycle(
        _FakeDb(),
        corpus_id="corpus-1",
        user_id="user-1",
        qdrant_client=object(),
        neo4j_driver=object(),
        apply=True,
        run_extraction_job_rows=True,
        run_graph_jobs=True,
        record_run=False,
    )

    assert calls == [
        "reconcile",
        "plan_source_parse",
        "plan_document_pipeline",
        "plan_graph",
        "plan_extraction",
        "plan_summary",
        "materialize",
    ]
    statuses = {step["name"]: step["status"] for step in result["steps"]}
    assert statuses["extraction_job_run"] == "skipped_pressure"
    assert statuses["graph_promotion_run"] == "skipped_pressure"
    assert result["summary"]["backpressure_blocked_steps"] == [
        "extraction_job_run",
        "graph_promotion_run",
    ]


@pytest.mark.asyncio
async def test_repair_cycle_backpressure_skips_source_parse_execution(monkeypatch):
    calls = []

    high_pressure = {
        "status": "needs_repair",
        "documents": {"queryable": 0, "total": 0},
        "summaries": {"body_parent_missing": 0, "document_missing": 0},
        "graph": {"pending": 0, "failed_chunks": 0, "stale_failure_rows": 0},
        "repair": {"source_parse_jobs": {"queued": 1}},
        "pressure": {
            "status": "high",
            "reasons": ["mongo_storage_over_stop_limit"],
            "backpressure": {"source_parse_allowed": False},
        },
    }

    async def fake_readiness(_db, _corpus_id):
        return high_pressure

    async def fake_materialize(_db, _corpus_id):
        calls.append("materialize")
        return {**high_pressure, "schema_version": "corpus_readiness.v1"}

    async def fake_plan_source_parse(*_args, **_kwargs):
        calls.append("plan_source_parse")
        return {"status": "complete", "planned": 1, "counts": {"queued": 1}}

    async def fail_run_source_parse(*_args, **_kwargs):
        raise AssertionError("source parse jobs must not run under pressure")

    monkeypatch.setattr(corpus_repair, "compute_corpus_readiness", fake_readiness)
    monkeypatch.setattr(corpus_repair, "materialize_corpus_readiness", fake_materialize)
    monkeypatch.setattr(corpus_repair, "plan_source_parse_jobs", fake_plan_source_parse)
    monkeypatch.setattr(corpus_repair, "run_source_parse_jobs", fail_run_source_parse)

    result = await corpus_repair.run_bounded_corpus_repair_cycle(
        _FakeDb(),
        corpus_id="corpus-1",
        user_id="user-1",
        apply=True,
        reconcile_failures=False,
        plan_source_parse_job_rows=True,
        run_source_parse_job_rows=True,
        plan_document_pipeline_job_rows=False,
        plan_graph_jobs=False,
        plan_extraction_job_rows=False,
        plan_summary_job_rows=False,
        record_run=False,
    )

    assert calls == ["plan_source_parse", "materialize"]
    statuses = {step["name"]: step["status"] for step in result["steps"]}
    assert statuses["source_parse_job_run"] == "skipped_pressure"
    assert result["summary"]["backpressure_blocked_steps"] == ["source_parse_job_run"]


@pytest.mark.asyncio
async def test_repair_cycle_rechecks_pressure_between_heavy_steps(monkeypatch):
    calls = []
    readiness_calls = 0

    normal_pressure = {
        "status": "needs_repair",
        "documents": {"queryable": 1, "total": 1},
        "summaries": {"body_parent_missing": 0, "document_missing": 0},
        "graph": {"pending": 1, "failed_chunks": 0, "stale_failure_rows": 0},
        "repair": {"graph_promotion_jobs": {"queued": 1}, "extraction_jobs": {"queued": 1}},
        "pressure": {
            "status": "normal",
            "backpressure": {
                "extraction_backfill_allowed": True,
                "graph_promotion_allowed": True,
            },
        },
    }
    high_pressure = {
        **normal_pressure,
        "pressure": {
            "status": "high",
            "reasons": ["mongo_storage_over_stop_limit"],
            "recommendations": ["pause_nonessential_backfills"],
            "backpressure": {
                "extraction_backfill_allowed": False,
                "graph_promotion_allowed": False,
            },
        },
    }

    async def fake_readiness(_db, _corpus_id):
        nonlocal readiness_calls
        readiness_calls += 1
        # Initial cycle snapshot and extraction gate are normal. Graph gate sees
        # the refreshed high-pressure state produced after extraction writes.
        return normal_pressure if readiness_calls <= 2 else high_pressure

    async def fake_materialize(_db, _corpus_id):
        calls.append("materialize")
        return {**high_pressure, "schema_version": "corpus_readiness.v1"}

    async def fake_plan_graph(*_args, **_kwargs):
        calls.append("plan_graph")
        return {"status": "complete", "planned": 1, "counts": {"queued": 1}}

    async def fake_plan_extraction(*_args, **_kwargs):
        calls.append("plan_extraction")
        return {"status": "complete", "planned": 1, "counts": {"queued": 1}}

    async def fake_run_extraction(*_args, **_kwargs):
        calls.append("run_extraction")
        return {"status": "complete", "claimed": 1, "counts": {"succeeded": 1}}

    async def fail_run_graph(*_args, **_kwargs):
        raise AssertionError("graph promotion must re-check pressure and skip")

    monkeypatch.setattr(corpus_repair, "compute_corpus_readiness", fake_readiness)
    monkeypatch.setattr(corpus_repair, "materialize_corpus_readiness", fake_materialize)
    monkeypatch.setattr(corpus_repair, "plan_graph_promotion_jobs", fake_plan_graph)
    monkeypatch.setattr(corpus_repair, "plan_extraction_jobs", fake_plan_extraction)
    monkeypatch.setattr(corpus_repair, "run_extraction_jobs", fake_run_extraction)
    monkeypatch.setattr(corpus_repair, "run_graph_promotion_jobs", fail_run_graph)

    result = await corpus_repair.run_bounded_corpus_repair_cycle(
        _FakeDb(),
        corpus_id="corpus-1",
        user_id="user-1",
        qdrant_client=object(),
        neo4j_driver=object(),
        apply=True,
        reconcile_failures=False,
        plan_source_parse_job_rows=False,
        plan_document_pipeline_job_rows=False,
        plan_graph_jobs=True,
        plan_extraction_job_rows=True,
        plan_summary_job_rows=False,
        run_extraction_job_rows=True,
        run_graph_jobs=True,
        record_run=False,
    )

    assert calls == [
        "plan_graph",
        "plan_extraction",
        "run_extraction",
        "plan_graph",
        "materialize",
    ]
    statuses = {step["name"]: step["status"] for step in result["steps"]}
    assert statuses["extraction_job_run"] == "complete"
    assert statuses["graph_promotion_run"] == "skipped_pressure"
    assert result["summary"]["backpressure_blocked_steps"] == ["graph_promotion_run"]


@pytest.mark.asyncio
async def test_repair_cycle_can_materialize_without_recording_nested_run(monkeypatch):
    calls = []

    async def fake_readiness(_db, _corpus_id):
        return {
            "status": "needs_repair",
            "documents": {"queryable": 1, "total": 1},
            "summaries": {"body_parent_missing": 0, "document_missing": 0},
            "graph": {"pending": 0, "failed_chunks": 1, "stale_failure_rows": 0},
            "repair": {"graph_promotion_jobs": {}, "source_parse_jobs": {}, "extraction_jobs": {}},
        }

    async def fake_materialize(_db, _corpus_id):
        calls.append("materialize")
        return {
            "status": "needs_repair",
            "documents": {"queryable": 1, "total": 1},
            "summaries": {"body_parent_missing": 0, "document_missing": 0},
            "graph": {"pending": 0, "failed_chunks": 1, "stale_failure_rows": 0},
            "repair": {"graph_promotion_jobs": {}, "source_parse_jobs": {}, "extraction_jobs": {}},
            "schema_version": "corpus_readiness.v1",
        }

    async def fake_reconcile(*_args, **_kwargs):
        return {"status": "complete"}

    monkeypatch.setattr(corpus_repair, "compute_corpus_readiness", fake_readiness)
    monkeypatch.setattr(corpus_repair, "materialize_corpus_readiness", fake_materialize)
    monkeypatch.setattr(corpus_repair, "reconcile_ghost_b_failure_metadata", fake_reconcile)

    db = _FakeDb()
    result = await corpus_repair.run_bounded_corpus_repair_cycle(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        apply=True,
        plan_source_parse_job_rows=False,
        plan_document_pipeline_job_rows=False,
        plan_graph_jobs=False,
        plan_extraction_job_rows=False,
        plan_summary_job_rows=False,
        record_run=False,
    )

    assert result["status"] == "complete"
    assert "run_id" not in result
    assert db.ingest_repair_runs.rows == []
    assert calls == ["materialize"]

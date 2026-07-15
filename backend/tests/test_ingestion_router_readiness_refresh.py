from unittest.mock import AsyncMock

import pytest

from routers import ingestion as router


@pytest.mark.asyncio
async def test_applied_extraction_job_plan_returns_fresh_readiness(monkeypatch):
    calls: list[str] = []

    async def fake_materialize(db, corpus_id):
        calls.append(corpus_id)
        return {"status": "fresh", "corpus_id": corpus_id}

    monkeypatch.setattr(
        "services.ingestion.readiness.materialize_corpus_readiness",
        fake_materialize,
    )
    monkeypatch.setattr(
        router.ingestion_service,
        "get_corpus",
        AsyncMock(return_value={"corpus_id": "corpus-1"}),
    )
    plan = AsyncMock(return_value={"status": "planned", "planned": 3})
    monkeypatch.setattr(router.ingestion_service, "plan_extraction_jobs", plan)

    result = await router.plan_extraction_jobs(
        "corpus-1",
        router.ExtractionJobPlanRequest(apply=True, limit=7),
        current_user={"user_id": "user-1"},
    )

    assert result["status"] == "planned"
    assert result["readiness"] == {"status": "fresh", "corpus_id": "corpus-1"}
    assert calls == ["corpus-1"]
    plan.assert_awaited_once()


@pytest.mark.asyncio
async def test_dry_run_extraction_job_plan_does_not_refresh_readiness(monkeypatch):
    async def fail_materialize(*_args, **_kwargs):
        raise AssertionError("dry-run planning should not rematerialize readiness")

    monkeypatch.setattr(
        "services.ingestion.readiness.materialize_corpus_readiness",
        fail_materialize,
    )
    monkeypatch.setattr(
        router.ingestion_service,
        "get_corpus",
        AsyncMock(return_value={"corpus_id": "corpus-1"}),
    )
    monkeypatch.setattr(
        router.ingestion_service,
        "plan_extraction_jobs",
        AsyncMock(return_value={"status": "dry_run", "planned": 3}),
    )

    result = await router.plan_extraction_jobs(
        "corpus-1",
        router.ExtractionJobPlanRequest(apply=False, limit=7),
        current_user={"user_id": "user-1"},
    )

    assert result == {"status": "dry_run", "planned": 3}


@pytest.mark.asyncio
async def test_summary_job_run_returns_fresh_readiness(monkeypatch):
    async def fake_materialize(_db, corpus_id):
        return {"status": "summaries_pending", "corpus_id": corpus_id}

    monkeypatch.setattr(
        "services.ingestion.readiness.materialize_corpus_readiness",
        fake_materialize,
    )
    monkeypatch.setattr(
        router.ingestion_service,
        "get_corpus",
        AsyncMock(return_value={"corpus_id": "corpus-1"}),
    )
    run_jobs = AsyncMock(return_value={"status": "complete", "claimed": 2})
    monkeypatch.setattr(
        router.ingestion_service,
        "run_summary_jobs",
        run_jobs,
    )

    result = await router.run_summary_jobs(
        "corpus-1",
        router.SummaryJobRunRequest(limit=2, summary_cost_authority_usd="1.00"),
        current_user={"user_id": "user-1"},
    )

    assert result["status"] == "complete"
    assert result["readiness"]["status"] == "summaries_pending"
    call = run_jobs.await_args.kwargs
    assert call["summary_cost_run_id"].startswith("summary_jobs_corpus-1_")
    assert str(call["summary_cost_authority_usd"]) == "1.00"

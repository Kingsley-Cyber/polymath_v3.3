"""Provider-not-required tests for the required summary baseline.

Owner decision: ``required_summary_provider: none``. The deterministic
baseline lane must never open ``summary_cost_control`` nor resolve
``summary_provider_pool``; cost authority belongs only to the optional
``llm_summary_enrichment.v1`` product.
"""

from __future__ import annotations

import pytest

from services.ingestion import document_summaries
from services.ingestion_service import IngestionService


def _forbid_cost_authority(monkeypatch) -> None:
    async def forbidden_open(*_args, **_kwargs):
        raise AssertionError(
            "deterministic baseline must never open summary cost authority"
        )

    monkeypatch.setattr(
        "services.ingestion.summary_cost_control.SummaryCostController.open",
        forbidden_open,
    )


def _forbid_provider_pool(monkeypatch) -> None:
    async def forbidden_pool(*_args, **_kwargs):
        raise AssertionError(
            "deterministic baseline must never resolve a summary provider pool"
        )

    monkeypatch.setattr(
        "services.ingestion.summary_provider_pool.resolve_summary_provider_pool",
        forbidden_pool,
    )
    monkeypatch.setattr(
        document_summaries, "_summary_tree_pool_for_corpus", forbidden_pool
    )


@pytest.mark.asyncio
async def test_service_document_backfill_deterministic_never_opens_cost_control(
    monkeypatch,
):
    service = IngestionService()
    service._db = object()
    downstream_kwargs: dict = {}

    async def no_pause(**_kwargs):
        return None

    async def no_materialize(_corpus_id):
        return None

    async def fake_downstream(db, **kwargs):
        downstream_kwargs.update(kwargs)
        return {"status": "complete", "attempted": 1, "built": 1, "skipped": 0, "failed": 0}

    monkeypatch.setattr(service, "_backpressure_pause_result", no_pause)
    monkeypatch.setattr(service, "_materialize_corpus_readiness_safely", no_materialize)
    monkeypatch.setattr(
        "services.ingestion.document_summaries.backfill_document_summaries",
        fake_downstream,
    )
    _forbid_cost_authority(monkeypatch)
    _forbid_provider_pool(monkeypatch)

    result = await service.backfill_document_summaries(
        corpus_id="corpus-1",
        user_id="user-1",
        limit=5,
        deterministic_only=True,
    )

    assert result["status"] == "complete"
    assert downstream_kwargs["deterministic_only"] is True
    assert downstream_kwargs["summary_cost_controller"] is None
    assert downstream_kwargs["require_cost_control"] is False
    receipt = result["summary_cost_receipt"]
    assert receipt["product"] == "deterministic_summary.v1"
    assert receipt["provider_calls"] == 0
    assert receipt["authority_required"] is False


@pytest.mark.asyncio
async def test_run_summary_jobs_without_authority_is_deterministic(monkeypatch):
    """No summary_cost_run_id -> deterministic lane: no controller opened,
    receipt reports zero provider calls."""

    service = IngestionService()
    service._db = object()
    job_kwargs: dict = {}

    async def no_readiness(_corpus_id):
        return None

    async def no_pause(**_kwargs):
        return None

    async def no_materialize(_corpus_id):
        return None

    async def owned_lane(*, runner, **_kwargs):
        return await runner()

    async def fake_run_jobs(db, **kwargs):
        job_kwargs.update(kwargs)
        # Exercise both runners the way the orchestrator would.
        parent_result = await kwargs["parent_runner"](limit=5, doc_ids=None)
        document_result = await kwargs["document_runner"](limit=5, doc_ids=None)
        return {
            "status": "complete",
            "corpus_id": kwargs["corpus_id"],
            "claimed": 2,
            "counts": {"succeeded": 2},
            "runner_results": {
                "retrieval_parent_summary": parent_result,
                "document_summary": document_result,
            },
            "jobs": [],
        }

    async def fake_deterministic_parents(db, *, corpus_id, limit=25, doc_ids=None):
        return {
            "status": "healthy",
            "corpus_id": corpus_id,
            "product": "deterministic_summary.v1",
            "generated": 1,
            "generation_errors": [],
        }

    async def fake_document_backfill(**kwargs):
        assert kwargs["deterministic_only"] is True
        return {
            "status": "complete",
            "attempted": 1,
            "built": 1,
            "skipped": 0,
            "failed": 0,
            "summary_cost_receipt": {
                "product": "deterministic_summary.v1",
                "provider_calls": 0,
                "authority_required": False,
            },
        }

    monkeypatch.setattr(service, "_compute_corpus_readiness_safely", no_readiness)
    monkeypatch.setattr(service, "_backpressure_pause_result", no_pause)
    monkeypatch.setattr(service, "_materialize_corpus_readiness_safely", no_materialize)
    monkeypatch.setattr(service, "_run_owned_repair_lane", owned_lane)
    monkeypatch.setattr(service, "_index_deterministic_parent_summaries",
                        lambda *_a, **_k: _zero())
    monkeypatch.setattr(
        "services.ingestion.deterministic_summary.run_deterministic_parent_summaries",
        fake_deterministic_parents,
    )
    monkeypatch.setattr(service, "backfill_document_summaries", fake_document_backfill)
    monkeypatch.setattr("services.ingestion.summary_jobs.run_summary_jobs", fake_run_jobs)
    _forbid_cost_authority(monkeypatch)

    result = await service.run_summary_jobs(
        corpus_id="corpus-1",
        user_id="user-1",
        limit=5,
    )

    assert result["status"] == "complete"
    receipt = result["summary_cost_receipt"]
    assert receipt["product"] == "deterministic_summary.v1"
    assert receipt["provider_calls"] == 0
    assert receipt["authority_required"] is False
    parent_runner_result = result["runner_results"]["retrieval_parent_summary"]
    assert parent_runner_result["product"] == "deterministic_summary.v1"


async def _zero():
    return 0

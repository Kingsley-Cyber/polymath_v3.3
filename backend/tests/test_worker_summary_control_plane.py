"""Worker summary control plane — deterministic default, cloud enrichment gated.

Owner ruling 2026-08-05 (CONTINUITY/INGESTION_CONTROL_PLANE_20260805.md):
required summaries = deterministic_summary.v1; cloud Ghost A
(llm_summary_enrichment.v1) runs only when summary_cost_controller is open.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.schemas import IngestionConfig, SourceTier, WriteState
from services.ingestion import worker
from services.ingestion.deterministic_summary import (
    DETERMINISTIC_SUMMARY_MODEL_STAMP,
    DETERMINISTIC_SUMMARY_SCHEMA_VERSION,
)


def _parent(*, parent_id: str = "p1", doc_id: str = "d1", corpus_id: str = "c1"):
    child = SimpleNamespace(
        chunk_id="ch1",
        chunk_kind="body",
        text="Infrastructure as code defines resources in versioned files.",
    )
    return SimpleNamespace(
        parent_id=parent_id,
        doc_id=doc_id,
        corpus_id=corpus_id,
        text=(
            "Infrastructure as code is a practice that defines compute and "
            "network resources in versioned files. Terraform is one tool in "
            "this space. Teams use modules to keep environments consistent."
        ),
        source_tier=SourceTier.tier_b,
        chunk_kind="body",
        heading_path=["Chapter 1", "Infrastructure as code"],
        source_hash="abc123sourcehash",
        children=[child],
    )


@pytest.mark.asyncio
async def test_fresh_summaries_without_cost_authority_use_deterministic_not_cloud():
    parent = _parent()

    with patch.object(
        worker.mongo_reader, "get_parent_chunks", new_callable=AsyncMock
    ) as parent_mock, patch.object(
        worker, "summarize_parents", new_callable=AsyncMock
    ) as summarize_mock, patch.object(worker.settings, "NEO4J_ENABLED", False):
        parent_mock.return_value = []
        result = await worker._run_ghosts_parallel(
            config=IngestionConfig(use_neo4j=False, chunk_summarization=True),
            parents=[parent],
            children=[],
            doc_id="d1",
            corpus_id="c1",
            model="deepseek/deepseek-v4-flash",
            db=AsyncMock(),
            qdrant_client=None,
            neo4j_driver=None,
            existing_doc=None,
            ws=WriteState(
                mongo_written=False,
                qdrant_written=False,
                neo4j_written=False,
            ),
            summary_cost_controller=None,
        )

    assert summarize_mock.await_count == 0
    assert result.summaries is not None
    assert len(result.summaries) == 1
    summary = result.summaries[0]
    assert summary.schema_version == DETERMINISTIC_SUMMARY_SCHEMA_VERSION
    assert summary.summary_model == DETERMINISTIC_SUMMARY_MODEL_STAMP
    assert summary.summary_type == "deterministic_parent"
    assert (summary.summary or "").strip()


@pytest.mark.asyncio
async def test_cost_authority_opens_deprecated_cloud_enrichment_lane():
    parent = _parent()
    fake_cloud = [
        worker.SummaryResult(
            parent_id="p1",
            doc_id="d1",
            corpus_id="c1",
            source_tier=SourceTier.tier_b,
            summary="cloud enrichment text",
            schema_version="llm_summary_enrichment.v1",
            summary_model="deepseek/deepseek-v4-flash",
        )
    ]
    cost = MagicMock(name="SummaryCostController")

    with patch.object(
        worker.mongo_reader, "get_parent_chunks", new_callable=AsyncMock
    ) as parent_mock, patch.object(
        worker, "summarize_parents", new_callable=AsyncMock
    ) as summarize_mock, patch(
        "services.ingestion.summary_provider_pool.resolve_summary_provider_pool",
        new_callable=AsyncMock,
    ) as pool_mock, patch.object(worker.settings, "NEO4J_ENABLED", False):
        parent_mock.return_value = []
        pool_mock.return_value = (
            [{"model": "deepseek/deepseek-v4-flash", "api_key": "x"}],
            {"primary_model": "deepseek/deepseek-v4-flash", "demoted_provider_count": 0},
        )
        summarize_mock.return_value = fake_cloud
        result = await worker._run_ghosts_parallel(
            config=IngestionConfig(use_neo4j=False, chunk_summarization=True),
            parents=[parent],
            children=[],
            doc_id="d1",
            corpus_id="c1",
            model="deepseek/deepseek-v4-flash",
            db=AsyncMock(),
            qdrant_client=None,
            neo4j_driver=None,
            existing_doc=None,
            ws=WriteState(
                mongo_written=False,
                qdrant_written=False,
                neo4j_written=False,
            ),
            summary_cost_controller=cost,
        )

    assert summarize_mock.await_count == 1
    assert summarize_mock.await_args.kwargs.get("cost_controller") is cost
    assert result.summaries is not None
    assert result.summaries[0].summary == "cloud enrichment text"

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.schemas import IngestionConfig
from services.extraction.graphify_pipeline import GraphifyPipelineOutput
from services.ghost_b import ExtractionBatchReport, ExtractionResult
from services.ingestion import worker
from services.ingestion.worker import WriteState
from services.ingestion_service import IngestionService


@pytest.mark.asyncio
async def test_public_ingestion_service_preserves_entrypoint_and_graphify_default() -> None:
    service = IngestionService()
    expected = SimpleNamespace(status="complete")
    run = AsyncMock(return_value=expected)
    with patch("services.ingestion.worker.run_ingest_job", run):
        result = await service.ingest(
            data=b"Graphify uses MongoDB.",
            filename="fixture.md",
            corpus_id="corpus",
            user_id="user",
            ingestion_config=IngestionConfig(chunk_summarization=False),
            model="unused",
            duplicate_policy="allow",
        )
    assert result is expected
    kwargs = run.await_args.kwargs
    assert kwargs["ingestion_config"].extraction_engine == "graphify_cpu"
    assert kwargs["data"] == b"Graphify uses MongoDB."


@pytest.mark.asyncio
async def test_worker_graphify_dispatch_passes_full_document_and_returns_results() -> None:
    child = SimpleNamespace(
        chunk_id="chunk-1", doc_id="doc", corpus_id="corpus",
        text="Graphify uses MongoDB.", chunk_kind="body", metadata={},
    )
    result = ExtractionResult(
        schema_version="polymath.extract.v1", chunk_id="chunk-1",
        doc_id="doc", corpus_id="corpus", entities=[], relations=[], facts=[],
    )
    pipeline_output = GraphifyPipelineOutput(
        report=ExtractionBatchReport(
            results=[result], failures=[], metrics={"engine": "graphify_cpu"},
        ),
        identity_digest="a" * 64,
        stage_receipts=(),
        resumed_stages=(),
    )
    run_pipeline = AsyncMock(return_value=pipeline_output)
    with (
        patch.object(worker.mongo_reader, "get_parent_chunks", AsyncMock(return_value=[])),
        patch.object(worker.mongo_reader, "read_ghost_b_staging", AsyncMock(return_value=None)),
        patch("services.extraction.graphify_pipeline.run_graphify_pipeline", run_pipeline),
        patch.object(worker.settings, "NEO4J_ENABLED", True),
    ):
        output = await worker._run_ghosts_parallel(
            config=IngestionConfig(
                extraction_engine="graphify_cpu", use_neo4j=True,
                chunk_summarization=False,
            ),
            parents=[], children=[child], doc_id="doc", corpus_id="corpus",
            model="unused", filename="fixture.md",
            document_text="Graphify uses MongoDB.",
            db=MagicMock(), qdrant_client=MagicMock(), neo4j_driver=MagicMock(),
            existing_doc=None, ws=WriteState(),
        )
    assert output.ghost_b_out == [result]
    assert output.ghost_b_metrics["engine"] == "graphify_cpu"
    kwargs = run_pipeline.await_args.kwargs
    assert kwargs["text"] == "Graphify uses MongoDB."
    assert kwargs["children"] == [child]

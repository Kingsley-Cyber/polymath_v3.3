"""
Smoke tests for the GHOST B staging feature (worker.py Phase 3 resume gate).

The staging list on each document is the authoritative signal for skipping
the Ghost B LLM call on retry. These tests mock ghost_b + embedder + Qdrant
+ Neo4j writers so we can exercise the worker's resume paths without a
live LLM. Mongo is still hit against the running test container.
"""

import asyncio
from dataclasses import asdict
from unittest.mock import AsyncMock, patch

import pytest

from services.conversation import conversation_service
from services.ghost_b import (
    EntityItem,
    ExtractionFailureItem,
    ExtractionResult,
    RelationItem,
)
from services.storage import mongo_reader, mongo_writer


def _sample_result(chunk_id: str) -> ExtractionResult:
    return ExtractionResult(
        schema_version="polymath.extract.v1",
        chunk_id=chunk_id,
        doc_id="doc-sample",
        corpus_id="corpus-sample",
        entities=[
            EntityItem(
                canonical_name="apple inc",
                surface_form="Apple Inc.",
                entity_type="Organization",
                confidence=0.95,
            )
        ],
        relations=[
            RelationItem(
                subject="apple inc",
                predicate="created_by",
                object="steve jobs",
                object_kind="entity",
                confidence=0.9,
            )
        ],
        entity_remap_count=0,
        entity_drop_count=0,
        relation_remap_count=0,
        relation_drop_count=0,
    )


@pytest.mark.asyncio
async def test_stash_then_read_roundtrip():
    """stash_ghost_b + read_ghost_b_staging preserve payload verbatim."""
    await conversation_service.connect()
    db = conversation_service._db
    try:
        doc_id = "unit-test-doc-stash"
        corpus_id = "unit-test-corpus-stash"
        # Seed a minimal document so the update target exists.
        await db["documents"].insert_one(
            {"doc_id": doc_id, "corpus_id": corpus_id, "placeholder": True}
        )
        try:
            results = [_sample_result("c1"), _sample_result("c2")]
            await mongo_writer.stash_ghost_b(db, doc_id, corpus_id, results)
            staged = await mongo_reader.read_ghost_b_staging(db, doc_id, corpus_id)
            assert staged is not None
            assert len(staged) == 2
            assert staged[0]["chunk_id"] == "c1"
            assert staged[0]["entities"][0]["entity_type"] == "Organization"
            assert staged[1]["relations"][0]["predicate"] == "created_by"
        finally:
            await db["documents"].delete_one(
                {"doc_id": doc_id, "corpus_id": corpus_id}
            )
    finally:
        await conversation_service.disconnect()


@pytest.mark.asyncio
async def test_stash_accepts_pre_serialized_dicts():
    """Ops may pass plain dicts (e.g. JSON imports) — must not require dataclass."""
    await conversation_service.connect()
    db = conversation_service._db
    try:
        doc_id = "unit-test-doc-dicts"
        corpus_id = "unit-test-corpus-dicts"
        await db["documents"].insert_one(
            {"doc_id": doc_id, "corpus_id": corpus_id, "placeholder": True}
        )
        try:
            raw = [
                {
                    "schema_version": "polymath.extract.v1",
                    "chunk_id": "c9",
                    "doc_id": doc_id,
                    "corpus_id": corpus_id,
                    "entities": [],
                    "relations": [],
                }
            ]
            await mongo_writer.stash_ghost_b(db, doc_id, corpus_id, raw)
            staged = await mongo_reader.read_ghost_b_staging(db, doc_id, corpus_id)
            assert staged == raw
        finally:
            await db["documents"].delete_one(
                {"doc_id": doc_id, "corpus_id": corpus_id}
            )
    finally:
        await conversation_service.disconnect()


@pytest.mark.asyncio
async def test_read_returns_none_when_field_absent():
    """Legacy documents that predate this feature must resolve to None."""
    await conversation_service.connect()
    db = conversation_service._db
    try:
        doc_id = "unit-test-doc-legacy"
        corpus_id = "unit-test-corpus-legacy"
        await db["documents"].insert_one(
            {"doc_id": doc_id, "corpus_id": corpus_id, "placeholder": True}
        )
        try:
            staged = await mongo_reader.read_ghost_b_staging(db, doc_id, corpus_id)
            assert staged is None
        finally:
            await db["documents"].delete_one(
                {"doc_id": doc_id, "corpus_id": corpus_id}
            )
    finally:
        await conversation_service.disconnect()


def test_rehydrate_ghost_b_staging_builds_nested_dataclasses():
    """Worker's rehydration helper must produce real EntityItem / RelationItem
    instances on the nested arrays, not leave them as dicts."""
    from services.ingestion.worker import _rehydrate_ghost_b_staging

    raw = [
        {
            "schema_version": "polymath.extract.v1",
            "chunk_id": "c1",
            "doc_id": "d1",
            "corpus_id": "corp1",
            "entities": [
                {
                    "canonical_name": "x",
                    "surface_form": "X",
                    "entity_type": "Concept",
                    "confidence": 0.8,
                }
            ],
            "relations": [
                {
                    "subject": "x",
                    "predicate": "related_to",
                    "object": "y",
                    "object_kind": "literal",
                    "confidence": 0.7,
                }
            ],
        }
    ]
    rehydrated = _rehydrate_ghost_b_staging(raw)
    assert len(rehydrated) == 1
    assert isinstance(rehydrated[0].entities[0], EntityItem)
    assert isinstance(rehydrated[0].relations[0], RelationItem)
    assert rehydrated[0].entities[0].entity_type == "Concept"


@pytest.mark.asyncio
async def test_resume_skip_reads_staging_not_llm():
    """Resume path: when staging exists, worker's _run_ghosts_parallel must
    read staging and NOT invoke ghost_b's extract_entities."""
    from models.schemas import IngestionConfig, WriteState
    from services.ingestion import worker

    db_stub = AsyncMock()
    existing_doc = {
        "doc_id": "d-resume", "corpus_id": "c-resume",
        "parent_chunks": [], "ghost_b_staging": [_sample_result("cX").__dict__ | {
            "entities": [{
                "canonical_name": "x", "surface_form": "X",
                "entity_type": "Person", "confidence": 0.9,
            }],
            "relations": [],
        }],
    }
    ws = WriteState(mongo_written=True, qdrant_written=True, neo4j_written=False)

    async def _read_stub(db, doc_id, corpus_id):
        return existing_doc["ghost_b_staging"]

    # use_neo4j=True, chunk_summarization=False to isolate Ghost B path
    cfg = IngestionConfig(use_neo4j=True, chunk_summarization=False)

    with patch.object(
        worker.mongo_reader, "read_ghost_b_staging", side_effect=_read_stub
    ) as read_mock, patch.object(
        worker, "extract_entities", new_callable=AsyncMock
    ) as extract_mock, patch.object(worker.settings, "NEO4J_ENABLED", True):
        summaries, ghost_b_out = await worker._run_ghosts_parallel(
            config=cfg, parents=[], children=[],
            doc_id="d-resume", corpus_id="c-resume",
            model="ollama/llama3.2:3b",
            db=db_stub, qdrant_client=None,
            neo4j_driver=object(),  # any non-None sentinel
            existing_doc=existing_doc, ws=ws,
        )

    assert read_mock.await_count == 1, "staging must be read exactly once"
    assert extract_mock.await_count == 0, "LLM must not be called on resume"
    assert ghost_b_out is not None and len(ghost_b_out) == 1
    assert isinstance(ghost_b_out[0], ExtractionResult)
    assert ghost_b_out[0].entities[0].entity_type == "Person"


@pytest.mark.asyncio
async def test_resume_skip_keeps_ghost_b_partial_metrics():
    """Reusing staged Ghost B output must not turn partial coverage into 100%."""
    from models.schemas import IngestionConfig, WriteState
    from services.ingestion import worker

    failure = ExtractionFailureItem(
        chunk_id="c-missing",
        doc_id="d-resume",
        corpus_id="c-resume",
        model="m",
        lane=0,
        attempts=3,
        error_type="parse_error",
        error_message="unterminated json",
    )
    existing_doc = {
        "doc_id": "d-resume",
        "corpus_id": "c-resume",
        "parent_chunks": [],
        "ghost_b_staging": [
            _sample_result("c-good").__dict__ | {
                "chunk_id": "c-good",
                "doc_id": "d-resume",
                "corpus_id": "c-resume",
                "entities": [{
                    "canonical_name": "x",
                    "surface_form": "X",
                    "entity_type": "Person",
                    "confidence": 0.9,
                }],
                "relations": [],
            }
        ],
        "ghost_b_failures": [asdict(failure)],
        # Simulates older/stale resume bookkeeping that only counted staged rows.
        "ghost_b_metrics": {
            "requested_chunks": 1,
            "extracted_chunks": 1,
            "failed_chunks": 0,
            "success_rate": 1.0,
            "error_counts": {},
        },
    }
    ws = WriteState(mongo_written=True, qdrant_written=True, neo4j_written=False)

    async def _read_stub(db, doc_id, corpus_id):
        return existing_doc["ghost_b_staging"]

    with patch.object(
        worker.mongo_reader, "read_ghost_b_staging", side_effect=_read_stub
    ), patch.object(
        worker, "extract_entities", new_callable=AsyncMock
    ) as extract_mock, patch.object(worker.settings, "NEO4J_ENABLED", True):
        result = await worker._run_ghosts_parallel(
            config=IngestionConfig(use_neo4j=True, chunk_summarization=False),
            parents=[],
            children=[],
            doc_id="d-resume",
            corpus_id="c-resume",
            model="ollama/llama3.2:3b",
            db=AsyncMock(),
            qdrant_client=None,
            neo4j_driver=object(),
            existing_doc=existing_doc,
            ws=ws,
        )

    assert extract_mock.await_count == 0
    assert result.ghost_b_failures == [failure]
    assert result.ghost_b_metrics is not None
    assert result.ghost_b_metrics["requested_chunks"] == 2
    assert result.ghost_b_metrics["extracted_chunks"] == 1
    assert result.ghost_b_metrics["failed_chunks"] == 1
    assert result.ghost_b_metrics["success_rate"] == 0.5
    assert result.ghost_b_metrics["error_counts"] == {"parse_error": 1}

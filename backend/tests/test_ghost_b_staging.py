"""
Smoke tests for the GHOST B staging feature (worker.py Phase 3 resume gate).

The staging list on each document is the authoritative signal for skipping
the Ghost B LLM call on retry. These tests mock ghost_b + embedder + Qdrant
+ Neo4j writers so we can exercise the worker's resume paths without a
live LLM. Mongo is still hit against the running test container.
"""

import asyncio
import os
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from bson import BSON

os.environ.setdefault("LITELLM_MASTER_KEY", "test-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-password")

from services.ghost_b import (
    EntityItem,
    ExtractionBatchReport,
    ExtractionFailureItem,
    ExtractionResult,
    FactItem,
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
        facts=[
            FactItem(
                subject="apple inc",
                fact_type="status",
                property_name="listing_status",
                value="public",
                unit=None,
                condition=None,
                confidence=0.8,
                evidence_phrase="Apple Inc. is public",
            )
        ],
        entity_remap_count=0,
        entity_drop_count=0,
        relation_remap_count=0,
        relation_drop_count=0,
    )


def _matches(row: dict, query: dict) -> bool:
    for key, expected in query.items():
        actual = row.get(key)
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif actual != expected:
            return False
    return True


class _FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, field, direction):
        reverse = direction < 0
        self.rows = sorted(self.rows, key=lambda row: row.get(field), reverse=reverse)
        return self

    async def to_list(self, length=None):
        return list(self.rows if length is None else self.rows[:length])


class _FakeCollection:
    def __init__(self):
        self.rows: list[dict] = []

    async def insert_one(self, row):
        self.rows.append(dict(row))

    async def find_one(self, query, projection=None):
        del projection
        for row in self.rows:
            if _matches(row, query):
                return dict(row)
        return None

    def find(self, query, projection=None):
        del projection
        return _FakeCursor([dict(row) for row in self.rows if _matches(row, query)])

    async def update_one(self, query, update):
        for row in self.rows:
            if _matches(row, query):
                for key, value in (update.get("$set") or {}).items():
                    row[key] = value
                for key in (update.get("$unset") or {}):
                    row.pop(key, None)
                return type("Result", (), {"modified_count": 1})()
        return type("Result", (), {"modified_count": 0})()

    async def delete_one(self, query):
        before = len(self.rows)
        self.rows = [row for row in self.rows if not _matches(row, query)]
        return type("Result", (), {"deleted_count": before - len(self.rows)})()

    async def delete_many(self, query):
        before = len(self.rows)
        self.rows = [row for row in self.rows if not _matches(row, query)]
        return type("Result", (), {"deleted_count": before - len(self.rows)})()

    async def count_documents(self, query):
        return sum(1 for row in self.rows if _matches(row, query))

    async def bulk_write(self, ops, ordered=False):
        del ordered
        for op in ops:
            existing = None
            for idx, row in enumerate(self.rows):
                if _matches(row, op._filter):
                    existing = idx
                    break
            if existing is None:
                self.rows.append(dict(op._doc))
            else:
                self.rows[existing] = dict(op._doc)
        return type("Result", (), {"bulk_api_result": {}})()


class _FakeDb(dict):
    def __init__(self):
        super().__init__(
            documents=_FakeCollection(),
            ghost_b_extractions=_FakeCollection(),
            parent_chunks=_FakeCollection(),
            chunks=_FakeCollection(),
        )


@pytest.mark.asyncio
async def test_stash_then_read_roundtrip():
    """stash_ghost_b + read_ghost_b_staging preserve payload verbatim."""
    db = _FakeDb()
    doc_id = "unit-test-doc-stash"
    corpus_id = "unit-test-corpus-stash"
    await db["documents"].insert_one(
        {"doc_id": doc_id, "corpus_id": corpus_id, "placeholder": True}
    )
    results = [_sample_result("c1"), _sample_result("c2")]
    await mongo_writer.stash_ghost_b(db, doc_id, corpus_id, results)
    staged = await mongo_reader.read_ghost_b_staging(db, doc_id, corpus_id)
    assert staged is not None
    assert len(staged) == 2
    assert staged[0]["chunk_id"] == "c1"
    assert staged[0]["entities"][0]["entity_type"] == "Organization"
    assert staged[1]["relations"][0]["predicate"] == "created_by"


@pytest.mark.asyncio
async def test_stash_accepts_pre_serialized_dicts():
    """Ops may pass plain dicts (e.g. JSON imports) — must not require dataclass."""
    db = _FakeDb()
    doc_id = "unit-test-doc-dicts"
    corpus_id = "unit-test-corpus-dicts"
    await db["documents"].insert_one(
        {"doc_id": doc_id, "corpus_id": corpus_id, "placeholder": True}
    )
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
    assert staged is not None
    assert len(staged) == 1
    assert staged[0]["chunk_id"] == "c9"
    assert staged[0]["entities"] == []
    assert staged[0]["relations"] == []
    doc = await db["documents"].find_one({"doc_id": doc_id, "corpus_id": corpus_id})
    assert doc.get("ghost_b_staging") is None
    assert doc.get("ghost_b_staging_count") == 1


@pytest.mark.asyncio
async def test_split_staging_keeps_document_compact_for_thousands_of_rows():
    """Thousands of Ghost B rows must not inflate the documents record."""
    db = _FakeDb()
    doc_id = "unit-test-doc-many-staging"
    corpus_id = "unit-test-corpus-many-staging"
    await db["documents"].insert_one(
        {"doc_id": doc_id, "corpus_id": corpus_id, "placeholder": True}
    )
    rows = [
        {
            "schema_version": "polymath.extract.v1",
            "chunk_id": f"chunk-{idx:04d}",
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "entities": [],
            "relations": [],
            "facts": [],
        }
        for idx in range(2500)
    ]
    await mongo_writer.stash_ghost_b(db, doc_id, corpus_id, rows)

    doc = await db["documents"].find_one({"doc_id": doc_id, "corpus_id": corpus_id})
    assert "ghost_b_staging" not in doc
    assert doc["ghost_b_staging_count"] == 2500
    assert len(BSON.encode(doc)) < 256_000
    assert await db["ghost_b_extractions"].count_documents(
        {"doc_id": doc_id, "corpus_id": corpus_id, "status": "ok"}
    ) == 2500


@pytest.mark.asyncio
async def test_read_ghost_b_staging_falls_back_to_legacy_inline():
    db = _FakeDb()
    doc_id = "unit-test-doc-inline-staging"
    corpus_id = "unit-test-corpus-inline-staging"
    legacy = [
        {
            "schema_version": "polymath.extract.v1",
            "chunk_id": "legacy-c1",
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "entities": [],
            "relations": [],
            "facts": [],
        }
    ]
    await db["documents"].insert_one(
        {
            "doc_id": doc_id,
            "corpus_id": corpus_id,
            "ghost_b_staging": legacy,
        }
    )
    staged = await mongo_reader.read_ghost_b_staging(db, doc_id, corpus_id)
    assert staged == legacy


@pytest.mark.asyncio
async def test_read_returns_none_when_field_absent():
    """Legacy documents that predate this feature must resolve to None."""
    db = _FakeDb()
    doc_id = "unit-test-doc-legacy"
    corpus_id = "unit-test-corpus-legacy"
    await db["documents"].insert_one(
        {"doc_id": doc_id, "corpus_id": corpus_id, "placeholder": True}
    )
    staged = await mongo_reader.read_ghost_b_staging(db, doc_id, corpus_id)
    assert staged is None


def test_rehydrate_ghost_b_staging_builds_nested_dataclasses():
    """Worker's rehydration helper must produce real nested dataclasses."""
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
            "facts": [
                {
                    "subject": "x",
                    "fact_type": "category",
                    "property_name": "topic",
                    "value": "systems",
                    "unit": None,
                    "condition": None,
                    "confidence": 0.8,
                    "evidence_phrase": "X is a systems topic",
                }
            ],
        }
    ]
    rehydrated = _rehydrate_ghost_b_staging(raw)
    assert len(rehydrated) == 1
    assert isinstance(rehydrated[0].entities[0], EntityItem)
    assert isinstance(rehydrated[0].relations[0], RelationItem)
    assert isinstance(rehydrated[0].facts[0], FactItem)
    assert rehydrated[0].entities[0].entity_type == "Concept"
    assert rehydrated[0].facts[0].property_name == "topic"


def test_rehydrate_ghost_b_staging_accepts_legacy_without_facts():
    from services.ingestion.worker import _rehydrate_ghost_b_staging

    raw = [
        {
            "schema_version": "polymath.extract.v1",
            "chunk_id": "c1",
            "doc_id": "d1",
            "corpus_id": "corp1",
            "entities": [],
            "relations": [],
        }
    ]

    rehydrated = _rehydrate_ghost_b_staging(raw)

    assert len(rehydrated) == 1
    assert rehydrated[0].facts == []


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
            "facts": [],
        }],
    }
    ws = WriteState(mongo_written=True, qdrant_written=True, neo4j_written=False)

    async def _read_stub(db, doc_id, corpus_id):
        return existing_doc["ghost_b_staging"]

    # use_neo4j=True, chunk_summarization=False to isolate Ghost B path
    cfg = IngestionConfig(use_neo4j=True, chunk_summarization=False)

    with patch.object(
        worker.mongo_reader, "get_parent_chunks", new_callable=AsyncMock
    ) as parent_mock, patch.object(
        worker.mongo_reader, "read_ghost_b_staging", side_effect=_read_stub
    ) as read_mock, patch.object(
        worker.mongo_reader, "read_ghost_b_failures", new_callable=AsyncMock
    ) as failures_mock, patch.object(
        worker, "extract_entities", new_callable=AsyncMock
    ) as extract_mock, patch.object(worker.settings, "NEO4J_ENABLED", True):
        parent_mock.return_value = []
        failures_mock.return_value = []
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
                "facts": [],
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

    async def _read_failures_stub(db, doc_id, corpus_id):
        return existing_doc["ghost_b_failures"]

    with patch.object(
        worker.mongo_reader, "get_parent_chunks", new_callable=AsyncMock
    ) as parent_mock, patch.object(
        worker.mongo_reader, "read_ghost_b_staging", side_effect=_read_stub
    ), patch.object(
        worker.mongo_reader, "read_ghost_b_failures", side_effect=_read_failures_stub
    ), patch.object(
        worker, "extract_entities", new_callable=AsyncMock
    ) as extract_mock, patch.object(worker.settings, "NEO4J_ENABLED", True):
        parent_mock.return_value = []
        result = await worker._run_ghosts_parallel(
            config=IngestionConfig(
                use_neo4j=True,
                chunk_summarization=False,
                extraction_engine="legacy_local",
            ),
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


@pytest.mark.asyncio
async def test_resume_extracts_only_missing_ghost_b_chunks():
    from models.schemas import IngestionConfig, WriteState
    from services.ingestion import worker

    staged = [
        _sample_result("c1").__dict__ | {
            "chunk_id": "c1",
            "doc_id": "d-resume",
            "corpus_id": "c-resume",
            "entities": [],
            "relations": [],
            "facts": [],
        }
    ]
    children = [
        SimpleNamespace(
            chunk_id="c1",
            doc_id="d-resume",
            corpus_id="c-resume",
            text="already extracted",
            chunk_kind="body",
            metadata={},
        ),
        SimpleNamespace(
            chunk_id="c2",
            doc_id="d-resume",
            corpus_id="c-resume",
            text="missing extraction",
            chunk_kind="body",
            metadata={},
        ),
    ]
    lens = SimpleNamespace(to_dict=lambda: {"id": "lens-1"})

    async def _extract_stub(tasks, **_kwargs):
        assert [task.chunk_id for task in tasks] == ["c2"]
        return ExtractionBatchReport(
            results=[
                ExtractionResult(
                    schema_version="polymath.extract.v1",
                    chunk_id="c2",
                    doc_id="d-resume",
                    corpus_id="c-resume",
                    entities=[],
                    relations=[],
                    facts=[],
                )
            ],
            failures=[],
            metrics={"requested_chunks": 1, "extracted_chunks": 1},
        )

    with patch.object(
        worker.mongo_reader, "get_parent_chunks", new_callable=AsyncMock
    ) as parent_mock, patch.object(
        worker.mongo_reader, "read_ghost_b_staging", new_callable=AsyncMock
    ) as staging_mock, patch.object(
        worker.mongo_reader, "read_ghost_b_failures", new_callable=AsyncMock
    ) as failures_mock, patch.object(
        worker, "get_or_create_schema_lens", new_callable=AsyncMock
    ) as lens_mock, patch.object(
        worker, "extract_entities", side_effect=_extract_stub
    ) as extract_mock, patch.object(worker.settings, "NEO4J_ENABLED", True):
        parent_mock.return_value = []
        staging_mock.return_value = staged
        failures_mock.return_value = []
        lens_mock.return_value = lens
        result = await worker._run_ghosts_parallel(
            config=IngestionConfig(
                use_neo4j=True,
                chunk_summarization=False,
                extraction_engine="legacy_local",
            ),
            parents=[],
            children=children,
            doc_id="d-resume",
            corpus_id="c-resume",
            model="ollama/test",
            db=AsyncMock(),
            qdrant_client=None,
            neo4j_driver=object(),
            existing_doc={"doc_id": "d-resume", "ghost_b_metrics": {}},
            ws=WriteState(mongo_written=True, qdrant_written=False, neo4j_written=False),
        )

    assert extract_mock.call_count == 1
    assert result.ghost_b_out is not None
    assert sorted(r.chunk_id for r in result.ghost_b_out) == ["c1", "c2"]


@pytest.mark.asyncio
async def test_resume_keeps_staged_ghost_b_when_missing_retry_totally_fails():
    from models.schemas import IngestionConfig, WriteState
    from services.ingestion import worker

    staged = [
        _sample_result("c1").__dict__ | {
            "chunk_id": "c1",
            "doc_id": "d-resume",
            "corpus_id": "c-resume",
            "entities": [],
            "relations": [],
            "facts": [],
        }
    ]
    children = [
        SimpleNamespace(
            chunk_id="c1",
            doc_id="d-resume",
            corpus_id="c-resume",
            text="already extracted",
            chunk_kind="body",
            metadata={},
        ),
        SimpleNamespace(
            chunk_id="c2",
            doc_id="d-resume",
            corpus_id="c-resume",
            text="missing extraction",
            chunk_kind="body",
            metadata={},
        ),
    ]
    lens = SimpleNamespace(to_dict=lambda: {"id": "lens-1"})

    async def _extract_stub(tasks, **_kwargs):
        assert [task.chunk_id for task in tasks] == ["c2"]
        return ExtractionBatchReport(
            results=[],
            failures=[
                ExtractionFailureItem(
                    chunk_id="c2",
                    doc_id="d-resume",
                    corpus_id="c-resume",
                    model="m",
                    lane=0,
                    attempts=2,
                    error_type="jsonl_contract",
                    error_message="bad tail",
                )
            ],
            metrics={"requested_chunks": 1, "extracted_chunks": 0, "failed_chunks": 1},
        )

    with patch.object(
        worker.mongo_reader, "get_parent_chunks", new_callable=AsyncMock
    ) as parent_mock, patch.object(
        worker.mongo_reader, "read_ghost_b_staging", new_callable=AsyncMock
    ) as staging_mock, patch.object(
        worker.mongo_reader, "read_ghost_b_failures", new_callable=AsyncMock
    ) as failures_mock, patch.object(
        worker, "get_or_create_schema_lens", new_callable=AsyncMock
    ) as lens_mock, patch.object(
        worker, "extract_entities", side_effect=_extract_stub
    ) as extract_mock, patch.object(worker.settings, "NEO4J_ENABLED", True):
        parent_mock.return_value = []
        staging_mock.return_value = staged
        failures_mock.return_value = []
        lens_mock.return_value = lens
        result = await worker._run_ghosts_parallel(
            config=IngestionConfig(
                use_neo4j=True,
                chunk_summarization=False,
                extraction_engine="legacy_local",
            ),
            parents=[],
            children=children,
            doc_id="d-resume",
            corpus_id="c-resume",
            model="ollama/test",
            db=AsyncMock(),
            qdrant_client=None,
            neo4j_driver=object(),
            existing_doc={"doc_id": "d-resume", "ghost_b_metrics": {}},
            ws=WriteState(mongo_written=True, qdrant_written=False, neo4j_written=False),
        )

    assert extract_mock.call_count == 1
    assert result.ghost_b_out is not None
    assert [r.chunk_id for r in result.ghost_b_out] == ["c1"]
    assert result.ghost_b_failures
    assert result.warnings[0].startswith("Ghost B graph extraction produced 0/1")


@pytest.mark.asyncio
async def test_resume_reuses_saved_parent_summaries_without_mongo_flag():
    from models.schemas import IngestionConfig, SourceTier, WriteState
    from services.ingestion import worker

    parent = SimpleNamespace(
        parent_id="p1",
        doc_id="d-resume",
        corpus_id="c-resume",
        text="parent text",
        source_tier=SourceTier.tier_b,
        chunk_kind="body",
    )
    parent_rows = [
        {
            "parent_id": "p1",
            "doc_id": "d-resume",
            "corpus_id": "c-resume",
            "summary": "saved summary",
        }
    ]

    with patch.object(
        worker.mongo_reader, "get_parent_chunks", new_callable=AsyncMock
    ) as parent_mock, patch.object(
        worker, "summarize_parents", new_callable=AsyncMock
    ) as summarize_mock, patch.object(worker.settings, "NEO4J_ENABLED", False):
        parent_mock.return_value = parent_rows
        result = await worker._run_ghosts_parallel(
            config=IngestionConfig(use_neo4j=False, chunk_summarization=True),
            parents=[parent],
            children=[],
            doc_id="d-resume",
            corpus_id="c-resume",
            model="ollama/test",
            db=AsyncMock(),
            qdrant_client=None,
            neo4j_driver=None,
            existing_doc={"doc_id": "d-resume"},
            ws=WriteState(mongo_written=False, qdrant_written=False, neo4j_written=False),
        )

    assert summarize_mock.await_count == 0
    assert result.summaries is not None
    assert result.summaries[0].summary == "saved summary"

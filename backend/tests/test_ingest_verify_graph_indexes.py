from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.ingestion import verify


class _FakeChunks:
    async def count_documents(self, _query):
        return 1

    async def find_one(self, _query, _projection=None):
        return {"chunk_id": "chunk-1"}


class _FakeDb:
    def __getitem__(self, name):
        if name == "chunks":
            return _FakeChunks()
        raise KeyError(name)


class _FakeQdrant:
    async def count(self, **_kwargs):
        return SimpleNamespace(count=1)

    async def scroll(self, **_kwargs):
        return [SimpleNamespace(payload={})], None


class _FakeNeo4jResult:
    async def single(self):
        return {"cnt": 1}


class _FakeNeo4jSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def run(self, *_args, **_kwargs):
        return _FakeNeo4jResult()


class _FakeNeo4jDriver:
    def session(self):
        return _FakeNeo4jSession()


@pytest.mark.asyncio
async def test_verify_ingest_checks_graph_retrieval_indexes(monkeypatch):
    wait_mock = AsyncMock(
        return_value={"entity_name_ft": "ONLINE", "fact_text_ft": "ONLINE"}
    )
    monkeypatch.setattr(
        "services.graph.schema.wait_for_retrieval_indexes",
        wait_mock,
    )
    monkeypatch.setattr(
        verify,
        "_expected_child_count",
        AsyncMock(return_value=1),
    )
    monkeypatch.setattr(
        verify,
        "_expected_summary_count",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        verify,
        "_verify_qdrant_text_contract",
        AsyncMock(return_value=[]),
    )

    ok, errors = await verify.verify_ingest(
        db=_FakeDb(),
        qdrant=_FakeQdrant(),
        neo4j_driver=_FakeNeo4jDriver(),
        doc_id="doc-1",
        corpus_id="corpus-12345678",
        target_qdrant_collections=["graph"],
        use_neo4j=True,
    )

    assert ok is True
    assert errors == []
    wait_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_ingest_fails_when_graph_retrieval_indexes_are_offline(
    monkeypatch,
):
    wait_mock = AsyncMock(side_effect=RuntimeError("fact_text_ft POPULATING"))
    monkeypatch.setattr(
        "services.graph.schema.wait_for_retrieval_indexes",
        wait_mock,
    )
    monkeypatch.setattr(
        verify,
        "_expected_child_count",
        AsyncMock(return_value=1),
    )
    monkeypatch.setattr(
        verify,
        "_expected_summary_count",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        verify,
        "_verify_qdrant_text_contract",
        AsyncMock(return_value=[]),
    )

    ok, errors = await verify.verify_ingest(
        db=_FakeDb(),
        qdrant=_FakeQdrant(),
        neo4j_driver=_FakeNeo4jDriver(),
        doc_id="doc-1",
        corpus_id="corpus-12345678",
        target_qdrant_collections=["graph"],
        use_neo4j=True,
    )

    assert ok is False
    assert any("neo4j.retrieval_indexes" in err for err in errors)
    assert any("fact_text_ft POPULATING" in err for err in errors)

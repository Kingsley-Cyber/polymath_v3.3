from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.ingestion import verify


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return self.rows


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
async def test_expected_child_count_includes_noisy_chunks_for_qdrant():
    class FakeChunks:
        def __init__(self):
            self.query = None

        async def count_documents(self, query):
            self.query = query
            return 2

    class FakeDb:
        def __init__(self):
            self.chunks = FakeChunks()

        def __getitem__(self, name):
            assert name == "chunks"
            return self.chunks

    db = FakeDb()

    count = await verify._expected_child_count(
        db,
        doc_id="doc-1",
        corpus_id="corpus-1",
        collection_kind="naive",
    )

    assert count == 2
    assert "$or" not in db.chunks.query


@pytest.mark.asyncio
async def test_expected_child_count_excludes_noisy_chunks_for_neo4j():
    class FakeChunks:
        def __init__(self):
            self.query = None

        async def count_documents(self, query):
            self.query = query
            return 1

    class FakeDb:
        def __init__(self):
            self.chunks = FakeChunks()

        def __getitem__(self, name):
            assert name == "chunks"
            return self.chunks

    db = FakeDb()

    count = await verify._expected_child_count(
        db,
        doc_id="doc-1",
        corpus_id="corpus-1",
        collection_kind="naive",
        exclude_noisy=True,
    )

    assert count == 1
    assert "$or" in db.chunks.query


@pytest.mark.asyncio
async def test_verify_ingest_checks_graph_retrieval_indexes(monkeypatch):
    wait_mock = AsyncMock(
        return_value={"entity_name_ft": "ONLINE", "fact_text_ft": "ONLINE"}
    )
    monkeypatch.setattr(
        "services.graph.schema.wait_for_retrieval_indexes",
        wait_mock,
    )
    expected_count_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(
        verify,
        "_expected_child_count",
        expected_count_mock,
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
    assert expected_count_mock.await_count == 2
    assert all(
        call.kwargs.get("exclude_noisy") is True
        for call in expected_count_mock.await_args_list
    )


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


@pytest.mark.asyncio
async def test_expected_qdrant_texts_use_summary_retrieval_text():
    class FakeCollection:
        def __init__(self, rows):
            self.rows = rows

        def find(self, _query, _projection=None):
            return _Cursor(self.rows)

    class FakeDb:
        def __getitem__(self, name):
            if name == "chunks":
                return FakeCollection(
                    [{"chunk_id": "child-1", "text": "Canonical child text."}]
                )
            if name == "parent_chunks":
                return FakeCollection(
                    [
                        {
                            "parent_id": "parent-1",
                            "summary": "Short generated summary.",
                            "retrieval_text": (
                                "Central claim: summaries improve recall.\n"
                                "Short generated summary.\n"
                                "Key points: child evidence anchors the parent."
                            ),
                        }
                    ]
                )
            raise KeyError(name)

    expected = await verify._expected_qdrant_texts(
        FakeDb(),
        doc_id="doc-1",
        corpus_id="corpus-1",
    )

    assert expected["child-1"] == "Canonical child text."
    assert expected["parent-1_summary"].startswith(
        "Central claim: summaries improve recall."
    )
    assert expected["parent-1_summary"] != "Short generated summary."


@pytest.mark.asyncio
async def test_expected_qdrant_texts_compile_missing_legacy_retrieval_text():
    class FakeCollection:
        def __init__(self, rows):
            self.rows = rows

        def find(self, _query, _projection=None):
            return _Cursor(self.rows)

    class FakeDb:
        def __getitem__(self, name):
            if name == "chunks":
                return FakeCollection([])
            if name == "parent_chunks":
                return FakeCollection(
                    [
                        {
                            "corpus_id": "corpus-1",
                            "doc_id": "doc-1",
                            "parent_id": "parent-1",
                            "text": "Source evidence for a legacy parent summary.",
                            "summary": "A legacy summary remains useful after its retrieval contract is compiled.",
                            "source_child_ids": ["child-1"],
                        }
                    ]
                )
            raise KeyError(name)

    expected = await verify._expected_qdrant_texts(
        FakeDb(),
        doc_id="doc-1",
        corpus_id="corpus-1",
    )

    assert expected["parent-1_summary"]
    assert "legacy summary" in expected["parent-1_summary"].lower()

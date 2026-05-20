from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.schemas import SourceChunk
from services.conversation import conversation_service
from services.retriever.hydrate import (
    hydrate_rerank_texts,
    hydrate_summary_rerank_texts,
)


class _FakeCursor:
    def __init__(self, records: list[dict]):
        self._records = records

    async def to_list(self, length=None):
        return self._records


class _FakeCollection:
    def __init__(self, records: list[dict]):
        self._records = records
        self.query = None
        self.projection = None

    def find(self, query, projection):
        self.query = query
        self.projection = projection
        wanted = set(query["chunk_id"]["$in"])
        corpora = set(query.get("corpus_id", {}).get("$in", []))
        records = [
            record
            for record in self._records
            if record.get("chunk_id") in wanted
            and (not corpora or record.get("corpus_id") in corpora)
        ]
        return _FakeCursor(records)


class _FakeDb:
    def __init__(self, records: list[dict], documents: list[dict] | None = None):
        self.chunks = _FakeCollection(records)
        self.documents = _FakeDocumentsCollection(documents or [])

    def __getitem__(self, name):
        if name == "chunks":
            return self.chunks
        if name == "documents":
            return self.documents
        raise AssertionError(name)


class _FakeDocumentsCollection:
    def __init__(self, records: list[dict]):
        self._records = records
        self.query = None
        self.projection = None

    def find(self, query, projection):
        self.query = query
        self.projection = projection
        doc_ids = set(query.get("doc_id", {}).get("$in", []))
        corpora = set(query.get("corpus_id", {}).get("$in", []))
        records = [
            record
            for record in self._records
            if (not doc_ids or record.get("doc_id") in doc_ids)
            and (not corpora or record.get("corpus_id") in corpora)
        ]
        return _FakeCursor(records)


def _chunk(chunk_id: str, text: str) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id="parent-1",
        doc_id="doc-1",
        corpus_id="corpus-1",
        text=text,
        score=0.5,
        source_tier="tier_b+lexical",
        chunk_kind="table",
    )


@pytest.mark.asyncio
async def test_hydrate_rerank_texts_replaces_qdrant_snippet(monkeypatch):
    full_text = (
        "Table: Table 1\n"
        "Columns: Architecture Need | Recommended AWS Service\n\n"
        "Row 4: Architecture Need=Event-driven processing; "
        "Recommended AWS Service=Amazon SQS + AWS Lambda"
    )
    fake_db = _FakeDb(
        [
            {
                "chunk_id": "chunk-1",
                "parent_id": "parent-1",
                "doc_id": "doc-1",
                "corpus_id": "corpus-1",
                "text": full_text,
                "chunk_kind": "table",
                "metadata": {"row_start": 1, "row_end": 7},
            }
        ]
    )
    monkeypatch.setattr(conversation_service, "_db", fake_db)

    original = [_chunk("chunk-1", "Table: Table 1\nRow 1: Static website hosting")]
    hydrated = await hydrate_rerank_texts(original, ["corpus-1"])

    assert hydrated[0].text == full_text
    assert "Event-driven processing" in hydrated[0].text
    assert "Amazon SQS + AWS Lambda" in hydrated[0].text
    assert hydrated[0].metadata == {"row_start": 1, "row_end": 7}
    assert original[0].text == "Table: Table 1\nRow 1: Static website hosting"


@pytest.mark.asyncio
async def test_hydrate_rerank_texts_leaves_summary_candidates_alone(monkeypatch):
    fake_db = _FakeDb([])
    monkeypatch.setattr(conversation_service, "_db", fake_db)

    original = [_chunk("parent-1_summary", "short summary")]
    hydrated = await hydrate_rerank_texts(original, ["corpus-1"])

    assert hydrated == original
    assert fake_db.chunks.query is None


@pytest.mark.asyncio
async def test_hydrate_summary_rerank_texts_replaces_summary_snippet(monkeypatch):
    full_summary = (
        "Table 1 maps event-driven processing to Amazon SQS + AWS Lambda "
        "because the queue decouples producers and Lambda scales consumers."
    )
    fake_db = _FakeDb(
        [],
        documents=[
            {
                "doc_id": "doc-1",
                "corpus_id": "corpus-1",
                "parent_chunks": [
                    {
                        "parent_id": "parent-1",
                        "summary": full_summary,
                        "heading_path": ["AWS Decision Chart"],
                        "chunk_kind": "table",
                        "metadata": {"table_index": 1},
                    }
                ],
            }
        ],
    )
    monkeypatch.setattr(conversation_service, "_db", fake_db)

    original = [_chunk("parent-1_summary", "Table 1 maps event-driven")]
    hydrated = await hydrate_summary_rerank_texts(original, ["corpus-1"])

    assert hydrated[0].text == full_summary
    assert hydrated[0].summary == full_summary
    assert hydrated[0].heading_path == ["AWS Decision Chart"]
    assert hydrated[0].chunk_kind == "table"
    assert hydrated[0].metadata == {"table_index": 1}
    assert original[0].text == "Table 1 maps event-driven"

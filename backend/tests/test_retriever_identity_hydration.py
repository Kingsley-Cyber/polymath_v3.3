import pytest

from models.schemas import SourceChunk
from services.retriever.hydrate import attach_document_identities


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return list(self.rows)


class _Collection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query, projection):
        return _Cursor(self.rows)


class _DB:
    def __init__(self):
        self.collections = {
            "chunks": _Collection(
                [{"chunk_id": "chunk-1", "parent_id": "parent-1", "doc_id": "doc-1"}]
            ),
            "documents": _Collection(
                [{"doc_id": "doc-1", "corpus_id": "corpus-1", "content_sha256": "hash-1"}]
            ),
        }

    def __getitem__(self, name):
        return self.collections[name]


@pytest.mark.asyncio
async def test_attach_document_identities_resolves_missing_doc_id(monkeypatch):
    from services.conversation import conversation_service

    monkeypatch.setattr(conversation_service, "_db", _DB())
    chunk = SourceChunk(
        chunk_id="chunk-1",
        parent_id="parent-1",
        doc_id="",
        corpus_id="corpus-1",
        text="evidence",
        score=0.5,
        source_tier="vector",
    )

    result = await attach_document_identities([chunk], ["corpus-1"])

    assert result[0].doc_id == "doc-1"
    assert result[0].metadata["source_file_hash"] == "hash-1"
    assert result[0].metadata["corpus_memberships"] == ["corpus-1"]

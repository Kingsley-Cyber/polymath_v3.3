from unittest.mock import AsyncMock

import pytest
from qdrant_client.models import PointStruct

from services.storage import qdrant_writer


class _CollectionClient:
    def __init__(self, *, exists_after_failure: bool) -> None:
        self.exists = False
        self.exists_after_failure = exists_after_failure
        self.create_calls = 0

    async def collection_exists(self, collection_name: str) -> bool:
        return self.exists

    async def create_collection(self, **kwargs) -> None:
        self.create_calls += 1
        if self.create_calls == 1:
            self.exists = self.exists_after_failure
            raise TimeoutError("read timed out")
        self.exists = True


class _PayloadIndexClient:
    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.create_calls = 0

    async def create_payload_index(self, **kwargs) -> None:
        self.create_calls += 1
        if self.failures:
            raise self.failures.pop(0)


class _UpsertClient:
    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.failures = failures or []
        self.calls: list[tuple[str, int]] = []
        self.batches: list[tuple[str, list[PointStruct]]] = []

    async def upsert(self, *, collection_name: str, points: list[PointStruct]) -> None:
        self.calls.append((collection_name, len(points)))
        self.batches.append((collection_name, list(points)))
        if self.failures:
            raise self.failures.pop(0)


@pytest.mark.asyncio
async def test_create_collection_accepts_server_side_success_after_timeout():
    client = _CollectionClient(exists_after_failure=True)

    await qdrant_writer._create_collection_with_retry(
        client,
        collection_name="corpus_abcd_naive",
        vectors_config={},
    )

    assert client.create_calls == 1


@pytest.mark.asyncio
async def test_create_collection_retries_when_timeout_did_not_create(monkeypatch):
    monkeypatch.setattr(qdrant_writer.asyncio, "sleep", AsyncMock())
    client = _CollectionClient(exists_after_failure=False)

    await qdrant_writer._create_collection_with_retry(
        client,
        collection_name="corpus_abcd_hrag",
        vectors_config={},
    )

    assert client.create_calls == 2


@pytest.mark.asyncio
async def test_payload_index_already_exists_is_idempotent():
    client = _PayloadIndexClient([RuntimeError("index already exists")])

    await qdrant_writer._create_payload_index_with_retry(
        client,
        collection_name="corpus_abcd_naive",
        field_name="corpus_id",
    )

    assert client.create_calls == 1


@pytest.mark.asyncio
async def test_payload_index_retries_transient_failure(monkeypatch):
    monkeypatch.setattr(qdrant_writer.asyncio, "sleep", AsyncMock())
    client = _PayloadIndexClient([TimeoutError("read timed out")])

    await qdrant_writer._create_payload_index_with_retry(
        client,
        collection_name="corpus_abcd_naive",
        field_name="doc_id",
    )

    assert client.create_calls == 2


@pytest.mark.asyncio
async def test_upsert_points_chunked_bounds_large_writes(monkeypatch):
    monkeypatch.setattr(qdrant_writer, "_QDRANT_UPSERT_BATCH_SIZE", 2)
    client = _UpsertClient()
    points = [
        PointStruct(id=str(i), vector=[0.1, 0.2], payload={"i": i})
        for i in range(5)
    ]

    await qdrant_writer._upsert_points_chunked(
        client, collection_name="corpus_abcd_naive", points=points
    )

    assert client.calls == [
        ("corpus_abcd_naive", 2),
        ("corpus_abcd_naive", 2),
        ("corpus_abcd_naive", 1),
    ]


@pytest.mark.asyncio
async def test_upsert_points_chunked_retries_transient_failure(monkeypatch):
    monkeypatch.setattr(qdrant_writer, "_QDRANT_UPSERT_BATCH_SIZE", 2)
    monkeypatch.setattr(qdrant_writer.asyncio, "sleep", AsyncMock())
    client = _UpsertClient([TimeoutError("read timed out")])
    points = [
        PointStruct(id=str(i), vector=[0.1, 0.2], payload={"i": i})
        for i in range(2)
    ]

    await qdrant_writer._upsert_points_chunked(
        client, collection_name="corpus_abcd_naive", points=points
    )

    assert client.calls == [("corpus_abcd_naive", 2), ("corpus_abcd_naive", 2)]


def test_graph_vector_text_and_embedding_ids_are_stable():
    entity_id = qdrant_writer.entity_embedding_id("corp-1", "Open AI")
    assert entity_id == qdrant_writer.entity_embedding_id("corp-1", "open ai")
    assert entity_id.startswith("entity:corp-1:")

    relation_id = qdrant_writer.relation_embedding_id(
        "corp-1", "doc-1", "chunk-1", "OpenAI", "created_by", "Sam Altman"
    )
    assert relation_id == qdrant_writer.relation_embedding_id(
        "corp-1", "doc-1", "chunk-1", "openai", "created by", "sam altman"
    )
    assert relation_id.startswith("relation:corp-1:doc-1:chunk-1:")

    entity_text = qdrant_writer.build_entity_vector_text(
        {
            "canonical_name": "openai",
            "display_name": "OpenAI",
            "aliases": ["Open AI"],
            "type": "Organization",
            "description": "AI research organization.",
        }
    )
    assert "OpenAI" in entity_text
    assert "Open AI" in entity_text
    assert "Organization" in entity_text

    relation_text = qdrant_writer.build_relation_vector_text(
        {
            "subject": "openai",
            "predicate": "created_by",
            "object": "sam altman",
            "predicate_family": "Provenance",
            "source_sentence": "OpenAI was co-founded by Sam Altman.",
        }
    )
    assert "created_by" in relation_text
    assert "Provenance" in relation_text
    assert "co-founded" in relation_text


@pytest.mark.asyncio
async def test_upsert_graph_entities_and_relations_use_graph_payload_shape(monkeypatch):
    monkeypatch.setattr(qdrant_writer, "_collection_layout", AsyncMock(return_value=(True, False)))
    monkeypatch.setattr(qdrant_writer, "_assert_collection_owner", AsyncMock())
    client = _UpsertClient()
    corpus_id = "abcd1234-corpus"

    await qdrant_writer.upsert_graph_entities(
        client,
        corpus_id,
        [
            {
                "embedding_id": qdrant_writer.entity_embedding_id(corpus_id, "openai"),
                "entity_id": "entity:openai",
                "canonical_name": "openai",
                "display_name": "OpenAI",
                "type": "Organization",
                "aliases": ["Open AI"],
                "description": "AI research organization.",
                "doc_ids": ["doc1"],
                "chunk_ids": ["chunk1"],
                "confidence": 0.91,
            }
        ],
        [[0.1, 0.2]],
    )
    await qdrant_writer.upsert_graph_relations(
        client,
        corpus_id,
        [
            {
                "embedding_id": qdrant_writer.relation_embedding_id(
                    corpus_id, "doc1", "chunk1", "openai", "created_by", "sam"
                ),
                "subject": "openai",
                "predicate": "created_by",
                "predicate_family": "Provenance",
                "object": "sam",
                "source_sentence": "OpenAI was created by Sam.",
                "doc_id": "doc1",
                "chunk_id": "chunk1",
            }
        ],
        [[0.3, 0.4]],
    )

    assert client.calls == [
        (qdrant_writer._col_for_corpus(corpus_id, "graph"), 1),
        (qdrant_writer._col_for_corpus(corpus_id, "graph"), 1),
    ]
    entity_payload = client.batches[0][1][0].payload
    relation_payload = client.batches[1][1][0].payload
    assert entity_payload["graph_kind"] == "entity"
    assert entity_payload["chunk_type"] == "entity"
    assert entity_payload["embedding_id"].startswith("entity:")
    assert relation_payload["graph_kind"] == "relation"
    assert relation_payload["chunk_type"] == "relation"
    assert relation_payload["relation_id"] == relation_payload["embedding_id"]

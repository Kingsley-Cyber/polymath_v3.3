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

    async def upsert(self, *, collection_name: str, points: list[PointStruct]) -> None:
        self.calls.append((collection_name, len(points)))
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

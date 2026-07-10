import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

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


def test_payload_text_contract_marks_full_text_not_preview():
    text = "Row 4: Event-driven processing uses Amazon SQS + AWS Lambda."

    contract = qdrant_writer.payload_text_contract(text)

    assert contract == {
        "text_len": len(text),
        "text_hash": hashlib.sha1(text.encode("utf-8")).hexdigest(),
        "is_truncated": False,
    }


class _ExistingCollectionClient:
    def __init__(self, *, dim: int = 1024) -> None:
        self.dim = dim
        self.created_indexes: list[tuple[str, str]] = []
        self.created_collections: list[str] = []

    async def collection_exists(self, collection_name: str) -> bool:
        return True

    async def get_collection(self, collection_name: str):
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors={"dense": SimpleNamespace(size=self.dim)}
                )
            )
        )

    async def create_payload_index(self, **kwargs) -> None:
        self.created_indexes.append((kwargs["collection_name"], kwargs["field_name"]))

    async def create_collection(self, **kwargs) -> None:
        self.created_collections.append(kwargs["collection_name"])


@pytest.mark.asyncio
async def test_existing_collections_still_get_payload_indexes_repaired():
    client = _ExistingCollectionClient()

    await qdrant_writer.ensure_collections_for_corpus(client, "abcdef123456", dim=1024)

    assert client.created_collections == []
    indexed_fields = {field for _collection, field in client.created_indexes}
    assert {"corpus_id", "doc_id", "chunk_id", "parent_id"} <= indexed_fields
    assert {"kind", "term"} <= indexed_fields


@pytest.mark.asyncio
async def test_existing_collection_dimension_mismatch_fails_loudly():
    client = _ExistingCollectionClient(dim=768)

    with pytest.raises(RuntimeError, match="vector dimension"):
        await qdrant_writer.ensure_collections_for_corpus(
            client,
            "abcdef123456",
            dim=1024,
        )


class _DeleteClient:
    def __init__(self) -> None:
        self.selectors = []

    async def collection_exists(self, _collection_name: str) -> bool:
        return True

    async def delete(self, *, points_selector, **_kwargs):
        self.selectors.append(points_selector)
        return SimpleNamespace(operation_id=1)


@pytest.mark.asyncio
async def test_doc_replace_can_preserve_existing_summary_points():
    client = _DeleteClient()

    await qdrant_writer.delete_points_by_doc(
        client,
        "abcdef123456",
        "doc-1",
        preserve_summary_points=True,
    )

    assert len(client.selectors) == 3
    for selector in client.selectors:
        assert selector.must_not
        assert selector.must_not[0].key == "chunk_type"
        assert selector.must_not[0].match.value == "summary"

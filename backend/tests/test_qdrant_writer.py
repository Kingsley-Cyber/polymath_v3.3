import asyncio
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


class _QueryPointsOnlyClient:
    def __init__(self) -> None:
        self.kwargs = None

    async def query_points(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(points=[SimpleNamespace(score=0.9)])


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
async def test_create_collection_forwards_binary_quantization_config():
    class CaptureClient:
        def __init__(self):
            self.kwargs = None

        async def collection_exists(self, _collection_name):
            return False

        async def create_collection(self, **kwargs):
            self.kwargs = kwargs

    client = CaptureClient()
    desired = qdrant_writer.binary_quantization_config()

    await qdrant_writer._create_collection_with_retry(
        client,
        collection_name="corpus_abcd_naive",
        vectors_config={},
        quantization_config=desired,
    )

    assert client.kwargs["quantization_config"] is desired


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
async def test_search_compat_uses_query_points_on_qdrant_client_118():
    client = _QueryPointsOnlyClient()
    query_filter = qdrant_writer.Filter(must=[])

    hits = await qdrant_writer._search_points_compat(
        client,
        collection_name="corpus_abcd_schemas",
        query_vector=[0.1, 0.2],
        query_filter=query_filter,
        limit=3,
        with_payload=True,
    )

    assert len(hits) == 1
    assert client.kwargs["query"] == [0.1, 0.2]
    assert client.kwargs["query_filter"] is query_filter
    quantization = client.kwargs["search_params"].quantization
    assert quantization.ignore is False
    assert quantization.rescore is True
    assert quantization.oversampling == 2.0


@pytest.mark.asyncio
async def test_collection_availability_positive_result_is_cached():
    class ExistsClient:
        def __init__(self):
            self.calls = 0

        async def collection_exists(self, _collection_name):
            self.calls += 1
            return True

    name = "corpus_existence_cache_schemas"
    qdrant_writer._COLLECTION_EXISTENCE_CACHE.discard(name)
    client = ExistsClient()

    assert await qdrant_writer._collection_available(client, name) is True
    assert await qdrant_writer._collection_available(client, name) is True
    assert client.calls == 1


@pytest.mark.asyncio
async def test_summary_tree_batch_search_preserves_query_order_and_filters():
    class BatchClient:
        def __init__(self):
            self.exists_calls = 0
            self.requests = []

        async def collection_exists(self, _collection_name):
            self.exists_calls += 1
            return True

        async def query_batch_points(self, *, requests, **_kwargs):
            self.requests = list(requests)
            return [
                SimpleNamespace(
                    points=[
                        SimpleNamespace(
                            score=0.9 - index * 0.1,
                            payload={"node_id": f"node-{index}"},
                        )
                    ]
                )
                for index, _request in enumerate(requests)
            ]

    corpus_id = "batch987654"
    name = qdrant_writer._col_for_corpus(corpus_id, "schemas")
    qdrant_writer._COLLECTION_EXISTENCE_CACHE.discard(name)
    client = BatchClient()

    rows = await qdrant_writer.search_summary_tree_entries_batch(
        client,
        corpus_id,
        queries=[
            {
                "query_vec": [0.1, 0.2],
                "doc_id": "doc-1",
                "node_type": "section",
                "top_k": 5,
            },
            {
                "query_vec": [0.3, 0.4],
                "doc_id": "doc-2",
                "node_type": "rollup",
                "node_ids": ["rollup-2"],
                "top_k": 3,
            },
        ],
    )

    assert [[row["node_id"] for row in result] for result in rows] == [
        ["node-0"],
        ["node-1"],
    ]
    assert client.exists_calls == 1
    assert len(client.requests) == 2
    assert client.requests[0].limit == 5
    assert client.requests[1].limit == 3
    assert client.requests[0].params.quantization.rescore is True
    assert client.requests[0].params.quantization.oversampling == 2.0


@pytest.mark.asyncio
async def test_lexicon_exact_lookup_uses_bounded_match_any_conditions():
    class ExactClient:
        def __init__(self):
            self.scroll_filter = None

        async def collection_exists(self, _collection_name):
            return True

        async def scroll(self, *, scroll_filter, **_kwargs):
            self.scroll_filter = scroll_filter
            return [], None

    client = ExactClient()
    await qdrant_writer.search_lexicon_entries(
        client,
        "abcdef123456",
        query_vec=None,
        exact_terms=["facs", "facial action coding system"],
    )

    assert len(client.scroll_filter.should) == 4
    assert all(
        condition.match.any == ["facs", "facial action coding system"]
        for condition in client.scroll_filter.should
    )


@pytest.mark.asyncio
async def test_lexicon_lookup_can_be_scoped_to_hierarchy_concept_ids():
    class ExactClient:
        def __init__(self):
            self.scroll_filter = None

        async def collection_exists(self, _collection_name):
            return True

        async def scroll(self, *, scroll_filter, **_kwargs):
            self.scroll_filter = scroll_filter
            return [], None

    client = ExactClient()
    await qdrant_writer.search_lexicon_entries(
        client,
        "abcdef123456",
        query_vec=None,
        exact_terms=["facs"],
        allowed_lexicon_ids=["lex-facs", "lex-laban"],
    )

    scoped = next(
        condition
        for condition in client.scroll_filter.must
        if condition.key == "lexicon_id"
    )
    assert scoped.match.any == ["lex-facs", "lex-laban"]


@pytest.mark.asyncio
async def test_retrieve_lexicon_entries_returns_payload_and_reusable_vector():
    class RetrieveClient:
        def __init__(self):
            self.ids = []

        async def collection_exists(self, _collection_name):
            return True

        async def retrieve(self, *, ids, **_kwargs):
            self.ids.extend(ids)
            return [
                SimpleNamespace(
                    payload={"lexicon_id": "lex-facs", "embedding_gloss": "FACS"},
                    vector=[0.1, 0.2],
                )
            ]

    client = RetrieveClient()
    rows = await qdrant_writer.retrieve_lexicon_entries(
        client,
        "abcdef123456",
        ["lex-facs", "lex-missing"],
    )

    assert len(client.ids) == 2
    assert rows == {
        "lex-facs": {
            "payload": {"lexicon_id": "lex-facs", "embedding_gloss": "FACS"},
            "vector": [0.1, 0.2],
        }
    }


@pytest.mark.asyncio
async def test_lexicon_dense_lookup_exposes_prefusion_rank(monkeypatch):
    class DenseClient:
        async def collection_exists(self, _collection_name):
            return True

    monkeypatch.setattr(
        qdrant_writer,
        "_search_points_compat",
        AsyncMock(
            return_value=[
                SimpleNamespace(
                    score=0.81,
                    payload={"lexicon_id": "lex-facs", "canonical_key": "facs"},
                ),
                SimpleNamespace(
                    score=0.76,
                    payload={"lexicon_id": "lex-laban", "canonical_key": "laban"},
                ),
            ]
        ),
    )

    rows = await qdrant_writer.search_lexicon_entries(
        DenseClient(),
        "abcdef123456",
        query_vec=[0.1, 0.2],
        top_k=4,
    )

    assert [(row["lexicon_id"], row["dense_rank"]) for row in rows] == [
        ("lex-facs", 1),
        ("lex-laban", 2),
    ]


def test_payload_text_contract_marks_full_text_not_preview():
    text = "Row 4: Event-driven processing uses Amazon SQS + AWS Lambda."

    contract = qdrant_writer.payload_text_contract(text)

    assert contract == {
        "text_len": len(text),
        "text_hash": hashlib.sha1(text.encode("utf-8")).hexdigest(),
        "is_truncated": False,
    }


class _ExistingCollectionClient:
    def __init__(
        self,
        *,
        dim: int = 1024,
        payload_indexes: set[str] | None = None,
        quantized: bool = True,
    ) -> None:
        self.dim = dim
        self.payload_indexes = payload_indexes or set()
        self.created_indexes: list[tuple[str, str]] = []
        self.created_collections: list[str] = []
        self.update_calls: list[str] = []
        self.quantization_configs: dict[str, object | None] = {}
        self.quantized = quantized

    async def collection_exists(self, collection_name: str) -> bool:
        return True

    async def get_collection(self, collection_name: str):
        quantization_config = self.quantization_configs.get(collection_name)
        if collection_name not in self.quantization_configs and self.quantized:
            quantization_config = qdrant_writer.binary_quantization_config()
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(
                    vectors={"dense": SimpleNamespace(size=self.dim)},
                    sparse_vectors={"sparse": object()},
                ),
                quantization_config=quantization_config,
            ),
            payload_schema={field: object() for field in self.payload_indexes},
        )

    async def create_payload_index(self, **kwargs) -> None:
        self.created_indexes.append((kwargs["collection_name"], kwargs["field_name"]))

    async def create_collection(self, **kwargs) -> None:
        self.created_collections.append(kwargs["collection_name"])

    async def update_collection(self, **kwargs) -> None:
        name = kwargs["collection_name"]
        self.update_calls.append(name)
        self.quantization_configs[name] = kwargs["quantization_config"]


@pytest.mark.asyncio
async def test_existing_collections_still_get_payload_indexes_repaired():
    client = _ExistingCollectionClient()

    await qdrant_writer.ensure_collections_for_corpus(client, "abcdef123456", dim=1024)

    assert client.created_collections == []
    indexed_fields = {field for _collection, field in client.created_indexes}
    assert {"corpus_id", "doc_id", "chunk_id", "parent_id"} <= indexed_fields
    assert {"kind", "term"} <= indexed_fields


@pytest.mark.asyncio
async def test_existing_collections_get_binary_quantization_reconciled_once():
    client = _ExistingCollectionClient(quantized=False)

    await qdrant_writer.ensure_collections_for_corpus(client, "abcdef123456", dim=1024)
    await qdrant_writer.ensure_collections_for_corpus(client, "abcdef123456", dim=1024)

    assert client.update_calls == [
        "corpus_abcdef12_naive",
        "corpus_abcdef12_hrag",
        "corpus_abcdef12_graph",
        "corpus_abcdef12_schemas",
    ]


@pytest.mark.asyncio
async def test_quantization_timeout_is_accepted_after_matching_readback():
    class TimeoutAfterApplyClient:
        def __init__(self):
            self.quantization_config = None
            self.calls = 0

        async def get_collection(self, _collection_name):
            return SimpleNamespace(
                config=SimpleNamespace(
                    quantization_config=self.quantization_config,
                )
            )

        async def update_collection(self, **kwargs):
            self.calls += 1
            self.quantization_config = kwargs["quantization_config"]
            raise TimeoutError("read timed out")

    client = TimeoutAfterApplyClient()

    changed = await qdrant_writer.ensure_binary_quantization(
        client,
        "corpus_abcd_naive",
    )

    assert changed is True
    assert client.calls == 1


@pytest.mark.asyncio
async def test_existing_payload_indexes_are_not_recreated_on_startup():
    client = _ExistingCollectionClient(
        payload_indexes=(
            set(qdrant_writer._CHUNK_PAYLOAD_INDEXES)
            | set(qdrant_writer._SCHEMA_PAYLOAD_INDEXES)
        )
    )

    await qdrant_writer.ensure_collections_for_corpus(client, "abcdef123456", dim=1024)

    assert client.created_collections == []
    assert client.created_indexes == []
    assert client.update_calls == []
    assert qdrant_writer._COLLECTION_LAYOUT_CACHE["corpus_abcdef12_naive"] == (
        True,
        True,
    )


@pytest.mark.asyncio
async def test_collection_layout_introspection_is_single_flight():
    class _LayoutClient:
        def __init__(self):
            self.calls = 0

        async def get_collection(self, _collection_name):
            self.calls += 1
            await asyncio.sleep(0)
            return SimpleNamespace(
                config=SimpleNamespace(
                    params=SimpleNamespace(
                        vectors={"dense": SimpleNamespace(size=1024)},
                        sparse_vectors={"sparse": object()},
                    )
                )
            )

    name = "corpus_singleflight_naive"
    qdrant_writer._COLLECTION_LAYOUT_CACHE.pop(name, None)
    qdrant_writer._COLLECTION_LAYOUT_LOCKS.pop(name, None)
    client = _LayoutClient()

    layouts = await asyncio.gather(
        *(qdrant_writer._collection_layout(client, name) for _ in range(12))
    )

    assert layouts == [(True, True)] * 12
    assert client.calls == 1


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

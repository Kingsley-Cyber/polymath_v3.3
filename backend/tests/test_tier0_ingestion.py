import pytest

from services.ingestion.tier0 import (
    _ensure_collection,
    delete_doc_profile,
    embed_doc_profiles,
)


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return self.rows[:length] if length is not None else list(self.rows)


class _Documents:
    def __init__(self, rows):
        self.rows = rows
        self.bulk_ops = []
        self.update_many_calls = []

    def find(self, query, _projection):
        ids = set(query["doc_id"]["$in"])
        return _Cursor([row for row in self.rows if row["doc_id"] in ids])

    async def bulk_write(self, operations, ordered=False):
        self.bulk_ops.extend(operations)

    async def update_many(self, query, update):
        self.update_many_calls.append((query, update))


class _Db:
    def __init__(self, rows):
        self.documents = _Documents(rows)

    def __getitem__(self, name):
        assert name == "documents"
        return self.documents


class _Qdrant:
    def __init__(self, *, exists=False):
        self.exists = exists
        self.created = []
        self.indexes = []
        self.points = []
        self.deleted = []
        self.updated = []
        self.quantization_config = None

    async def collection_exists(self, _name):
        return self.exists

    async def create_collection(self, **kwargs):
        self.created.append(kwargs)
        self.exists = True
        self.quantization_config = kwargs.get("quantization_config")

    async def get_collection(self, _collection_name):
        return type(
            "CollectionInfo",
            (),
            {
                "config": type(
                    "Config",
                    (),
                    {"quantization_config": self.quantization_config},
                )()
            },
        )()

    async def update_collection(self, **kwargs):
        self.updated.append(kwargs)
        self.quantization_config = kwargs["quantization_config"]

    async def create_payload_index(self, **kwargs):
        self.indexes.append(kwargs)

    async def upsert(self, *, collection_name, points, wait=False):
        self.points.extend(points)

    async def delete(self, **kwargs):
        self.deleted.append(kwargs)


@pytest.mark.asyncio
async def test_tier0_profiles_embed_in_one_batch_and_stamp_projection(monkeypatch):
    db = _Db(
        [
            {
                "doc_id": "doc-1",
                "title": "First",
                "source_type": "markdown",
                "doc_profile": {"summary": "First summary.", "concepts": ["one"]},
            },
            {
                "doc_id": "doc-2",
                "title": "Second",
                "source_type": "markdown",
                "doc_profile": {"summary": "Second summary.", "concepts": ["two"]},
            },
        ]
    )
    qdrant = _Qdrant()
    calls = []

    async def fake_embed(texts, *, expected_dim, api_key=None):
        calls.append((texts, expected_dim, api_key))
        return [[0.1] * expected_dim for _ in texts]

    monkeypatch.setattr("services.embedder.embed_batch", fake_embed)

    result = await embed_doc_profiles(
        db,
        qdrant,
        corpus_id="corpus-1",
        doc_ids=["doc-1", "doc-2"],
        dim=4,
    )

    assert result["embedded"] == 2
    assert calls == [(["First summary.", "Second summary."], 4, None)]
    assert len(qdrant.points) == 2
    assert qdrant.created[0]["quantization_config"].binary.always_ram is True
    assert qdrant.updated == []
    assert {point.payload["doc_id"] for point in qdrant.points} == {"doc-1", "doc-2"}
    assert len(db.documents.bulk_ops) == 2
    assert all(
        operation._doc["$set"]["write_state.document_profile_indexed"] is True
        for operation in db.documents.bulk_ops
    )


@pytest.mark.asyncio
async def test_tier0_profile_delete_targets_deterministic_shared_point():
    qdrant = _Qdrant(exists=True)

    await delete_doc_profile(qdrant, corpus_id="corpus-1", doc_id="doc-1")

    assert len(qdrant.deleted) == 1
    assert qdrant.deleted[0]["collection_name"] == "polymath_doc_summaries"
    assert len(qdrant.deleted[0]["points_selector"].points) == 1


@pytest.mark.asyncio
async def test_existing_tier0_collection_reconciles_binary_quantization_once():
    qdrant = _Qdrant(exists=True)

    await _ensure_collection(qdrant, 1024)
    await _ensure_collection(qdrant, 1024)

    assert len(qdrant.updated) == 1
    assert qdrant.updated[0]["collection_name"] == "polymath_doc_summaries"
    assert qdrant.quantization_config.binary.always_ram is True

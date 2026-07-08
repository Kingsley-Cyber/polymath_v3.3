import pytest

from services.ingestion_service import IngestionService
from services.storage.mongo_writer import delete_corpus
from services.storage.mongo_writer import replace_relation_support_for_document
from services.storage.record_status import (
    ACTIVE_STATUS,
    DELETED_STATUS,
    active_record_clause,
    with_active_records,
)


def test_active_record_clause_allows_legacy_missing_status():
    assert active_record_clause() == {
        "$or": [
            {"status": {"$exists": False}},
            {"status": ACTIVE_STATUS},
        ]
    }


def test_with_active_records_wraps_existing_query():
    assert with_active_records({"corpus_id": "c1"}) == {
        "$and": [
            {"corpus_id": "c1"},
            {
                "$or": [
                    {"status": {"$exists": False}},
                    {"status": ACTIVE_STATUS},
                ]
            },
        ]
    }


class _UpdateResult:
    matched_count = 1
    modified_count = 1


class _FakeCollection:
    def __init__(self, *, fail_update_many: bool = False):
        self.update_many_calls = []
        self.update_one_calls = []
        self.bulk_ops = []
        self.fail_update_many = fail_update_many

    async def update_many(self, query, update):
        if self.fail_update_many:
            raise RuntimeError("simulated update_many failure")
        self.update_many_calls.append((query, update))
        return _UpdateResult()

    async def update_one(self, query, update):
        self.update_one_calls.append((query, update))
        return _UpdateResult()

    async def bulk_write(self, ops, ordered=False):
        self.bulk_ops.extend(ops)
        self.ordered = ordered
        return None


class _FakeDb:
    def __init__(self, collections=None):
        self.collections = collections or {"relation_support_records": _FakeCollection()}

    def __getitem__(self, name):
        return self.collections[name]


@pytest.mark.asyncio
async def test_replace_relation_support_for_document_tombstones_then_upserts_active_rows():
    db = _FakeDb()

    count = await replace_relation_support_for_document(
        db,
        doc_id="doc-1",
        corpus_id="corpus-1",
        records=[
            {
                "support_id": "support-1",
                "edge_key": "entity:a|uses|entity:b",
                "source_entity_id": "entity:a",
                "predicate": "uses",
                "target_entity_id": "entity:b",
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "parent_id": "parent-1",
                "chunk_id": "chunk-1",
            }
        ],
    )

    collection = db["relation_support_records"]
    assert count == 1
    assert collection.update_many_calls[0][0] == {
        "doc_id": "doc-1",
        "corpus_id": "corpus-1",
    }
    assert collection.update_many_calls[0][1]["$set"]["status"] == DELETED_STATUS
    assert len(collection.bulk_ops) == 1
    op = collection.bulk_ops[0]
    assert op._filter == {"support_id": "support-1"}
    assert op._doc["$set"]["status"] == ACTIVE_STATUS
    assert op._doc["$unset"] == {"deleted_at": ""}
    assert op._upsert is True


@pytest.mark.asyncio
async def test_delete_corpus_unsets_deleting_marker():
    corpora = _FakeCollection()
    db = _FakeDb({"corpora": corpora})

    assert await delete_corpus(db, "corpus-1") is True

    query, update = corpora.update_one_calls[0]
    assert query == {"corpus_id": "corpus-1"}
    assert update["$set"]["status"] == DELETED_STATUS
    assert "deleted_at" in update["$set"]
    assert update["$unset"] == {"deleting_at": ""}


@pytest.mark.asyncio
async def test_background_purge_finalizes_corpus_when_chunk_cleanup_fails():
    service = IngestionService()
    corpora = _FakeCollection()
    chunks = _FakeCollection(fail_update_many=True)
    service._db = _FakeDb({"corpora": corpora, "chunks": chunks})
    service._settings.NEO4J_ENABLED = False

    await service._purge_corpus_bulk("corpus-1")

    tombstone_update = corpora.update_one_calls[0][1]
    cleanup_update = corpora.update_one_calls[1][1]
    assert tombstone_update["$set"]["status"] == DELETED_STATUS
    assert tombstone_update["$unset"] == {"deleting_at": ""}
    assert cleanup_update["$set"]["cleanup_status"] == "partial"
    assert cleanup_update["$set"]["cleanup_warnings"][0]["stage"] == "mongo_chunks"

import pytest

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
    def __init__(self):
        self.update_many_calls = []
        self.bulk_ops = []

    async def update_many(self, query, update):
        self.update_many_calls.append((query, update))
        return _UpdateResult()

    async def bulk_write(self, ops, ordered=False):
        self.bulk_ops.extend(ops)
        self.ordered = ordered
        return None


class _FakeDb:
    def __init__(self):
        self.collections = {"relation_support_records": _FakeCollection()}

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

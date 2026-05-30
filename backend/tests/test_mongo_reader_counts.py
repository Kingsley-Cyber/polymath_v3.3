from __future__ import annotations

import os
from collections import Counter

import pytest

os.environ.setdefault("LITELLM_MASTER_KEY", "test-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-password")

from services.storage import mongo_reader


class _Cursor:
    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self._skip = 0
        self._limit = None

    def sort(self, *_args):
        return self

    def skip(self, offset):
        self._skip = offset
        return self

    def limit(self, limit):
        self._limit = limit
        return self

    async def to_list(self, length=None):
        limit = length or self._limit
        rows = self.rows[self._skip :]
        return rows[:limit] if limit else rows

    def __aiter__(self):
        self._iter = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _Collection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query, projection=None):
        del projection
        rows = [
            row
            for row in self.rows
            if row.get("corpus_id") == query.get("corpus_id")
            and (
                "user_id" not in query
                or row.get("user_id") == query.get("user_id")
            )
        ]
        return _Cursor(rows)

    def aggregate(self, pipeline):
        match = pipeline[0]["$match"]
        doc_ids = set(match["doc_id"]["$in"])
        counts = Counter(
            row["doc_id"]
            for row in self.rows
            if row.get("corpus_id") == match["corpus_id"]
            and row.get("doc_id") in doc_ids
        )
        return _Cursor([{"_id": doc_id, "count": count} for doc_id, count in counts.items()])


class _Db:
    def __init__(self):
        self.collections = {
            "documents": _Collection(
                [
                    {"doc_id": "d1", "corpus_id": "c1", "user_id": "u1", "parent_count": 0},
                    {"doc_id": "d2", "corpus_id": "c1", "user_id": "u1", "parent_count": 9},
                ]
            ),
            "chunks": _Collection(
                [
                    {"doc_id": "d1", "corpus_id": "c1"},
                    {"doc_id": "d1", "corpus_id": "c1"},
                    {"doc_id": "d2", "corpus_id": "c1"},
                ]
            ),
            "parent_chunks": _Collection(
                [
                    {"doc_id": "d1", "corpus_id": "c1"},
                    {"doc_id": "d1", "corpus_id": "c1"},
                ]
            ),
        }

    def __getitem__(self, name):
        return self.collections[name]


@pytest.mark.asyncio
async def test_list_documents_injects_child_and_parent_counts():
    rows = await mongo_reader.list_documents(
        _Db(),
        "c1",
        user_id="u1",
        limit=10,
    )

    by_doc = {row["doc_id"]: row for row in rows}
    assert by_doc["d1"]["chunk_count"] == 2
    assert by_doc["d1"]["parent_count"] == 2
    assert by_doc["d2"]["chunk_count"] == 1
    assert by_doc["d2"]["parent_count"] == 9

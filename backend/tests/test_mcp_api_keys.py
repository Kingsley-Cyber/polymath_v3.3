"""User-scoped MCP API key tests."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from polymath_mcp import key_store


class _AsyncCursor:
    def __init__(self, docs):
        self.docs = list(docs)

    def sort(self, field, direction):
        reverse = direction < 0
        self.docs.sort(key=lambda d: d.get(field), reverse=reverse)
        return self

    def __aiter__(self):
        self._iter = iter(self.docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _Collection:
    def __init__(self):
        self.docs = []

    async def create_index(self, *_args, **_kwargs):
        return None

    async def insert_one(self, doc):
        self.docs.append(dict(doc))
        return SimpleNamespace(inserted_id="id")

    async def find_one(self, query, _projection=None):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                return dict(doc)
        return None

    async def update_one(self, query, update):
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                doc.update(update.get("$set", {}))
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    def find(self, query, projection=None):
        rows = []
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in query.items()):
                row = dict(doc)
                if projection:
                    for field, include in projection.items():
                        if include == 0:
                            row.pop(field, None)
                rows.append(row)
        return _AsyncCursor(rows)


class _Db(dict):
    def __getitem__(self, name):
        if name not in self:
            self[name] = _Collection()
        return dict.__getitem__(self, name)


@pytest.mark.asyncio
async def test_user_mcp_key_is_shown_once_hashed_listed_and_revoked():
    db = _Db()

    created = await key_store.create_mcp_key(db, user_id="user-1", name="Cursor")

    assert created["api_key"].startswith("pmcp_")
    assert created["name"] == "Cursor"
    stored = db[key_store.COLLECTION].docs[0]
    assert stored["token_hash"] == key_store.hash_mcp_key(created["api_key"])
    assert created["api_key"] not in str(stored)

    listed = await key_store.list_mcp_keys(db, user_id="user-1")
    assert listed == [
        {
            "key_id": created["key_id"],
            "name": "Cursor",
            "prefix": created["prefix"],
            "created_at": created["created_at"],
            "last_used_at": None,
            "revoked_at": None,
            "scope": "user",
        }
    ]

    assert await key_store.validate_user_mcp_key(db, created["api_key"]) == "user-1"
    assert db[key_store.COLLECTION].docs[0]["last_used_at"] is not None

    assert await key_store.revoke_mcp_key(
        db, user_id="user-1", key_id=created["key_id"]
    )
    assert await key_store.validate_user_mcp_key(db, created["api_key"]) is None
    assert await key_store.list_mcp_keys(db, user_id="user-1") == []

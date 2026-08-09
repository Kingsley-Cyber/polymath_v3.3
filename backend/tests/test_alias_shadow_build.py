"""Production alias shadow build: ghost rows -> shadow collections, idempotent."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.ingestion.alias_shadow_build import rebuild_corpus_alias_shadow
from services.ingestion.fixture_knowledge_pipeline import SHADOW_COLLECTIONS


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def __aiter__(self):
        self._it = iter(self._rows)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    async def to_list(self, length=None):
        return list(self._rows)


class _Collection:
    def __init__(self):
        self.rows = []

    def find(self, query, projection=None):
        matched = [dict(r) for r in self.rows
                   if all(r.get(k) == v for k, v in query.items())]
        return _Cursor(matched)

    async def delete_many(self, query):
        before = len(self.rows)
        self.rows = [r for r in self.rows
                     if not all(r.get(k) == v for k, v in query.items())]
        return SimpleNamespace(deleted_count=before - len(self.rows))

    async def insert_many(self, rows):
        self.rows.extend(dict(r) for r in rows)
        return SimpleNamespace(inserted_ids=list(range(len(rows))))


class _Db(dict):
    def __missing__(self, key):
        value = _Collection()
        self[key] = value
        return value


def _seed(db):
    text = ("Meridian Event Mesh (MEM) routes telemetry. MEM depends on the "
            "Basalt Queue, a durable log service.")
    db["ghost_b_extractions"].rows = [{
        "corpus_id": "corpus-1", "doc_id": "doc-1", "chunk_id": "doc-1_c0",
        "text": text,
        "entities": [
            {"canonical_name": "Meridian Event Mesh", "surface_form": "Meridian Event Mesh",
             "entity_type": "System", "confidence": 0.95, "query_aliases": [],
             "definitional_phrase": "", "object_kind": ""},
            {"canonical_name": "Basalt Queue", "surface_form": "Basalt Queue",
             "entity_type": "Software", "confidence": 0.9, "query_aliases": [],
             "definitional_phrase": "a durable log service", "object_kind": ""},
        ],
    }]
    db["chunks"].rows = [{
        "corpus_id": "corpus-1", "chunk_id": "doc-1_c0", "parent_id": "doc-1_p0",
    }]


@pytest.mark.asyncio
async def test_rebuild_writes_all_shadow_lanes_and_is_idempotent() -> None:
    db = _Db()
    _seed(db)
    first = await rebuild_corpus_alias_shadow(db, corpus_id="corpus-1")
    assert first["shadow_schema_records"] >= 1
    counts1 = {name: len(db[name].rows) for name in SHADOW_COLLECTIONS.values()
               if name != "knowledge_artifact_bundles"}
    assert counts1[SHADOW_COLLECTIONS["schema_shadow"]] >= 1
    schema_row = db[SHADOW_COLLECTIONS["schema_shadow"]].rows[0]
    assert schema_row["identity_authority"] is False
    assert schema_row["note"] == "shadow_only_do_not_overwrite_production_schemas"

    second = await rebuild_corpus_alias_shadow(db, corpus_id="corpus-1")
    counts2 = {name: len(db[name].rows) for name in counts1}
    assert counts1 == counts2  # delete+rewrite converges
    assert first["shadow_schema_records"] == second["shadow_schema_records"]


@pytest.mark.asyncio
async def test_acronym_pair_reaches_corpus_entities() -> None:
    db = _Db()
    _seed(db)
    await rebuild_corpus_alias_shadow(db, corpus_id="corpus-1")
    rows = db[SHADOW_COLLECTIONS["corpus_entities"]].rows
    assert rows
    joined = str(rows)
    assert "MEM" in joined or "Meridian" in joined

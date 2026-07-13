"""Safe integration coverage for the S2 document-only backfill.

These tests use an in-memory async Mongo facsimile and temporary backup paths.
They never connect to, name, or mutate a production corpus.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.bibliographic_backfill import (
    BACKFILL_ORIGIN,
    plan_for_document,
    restore_backup,
    run_corpus,
)


_MISSING = object()


def _value(row: dict, dotted: str):
    value = row
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _matches(row: dict, query: dict) -> bool:
    if "$and" in query:
        return all(_matches(row, clause) for clause in query["$and"])
    for key, expected in query.items():
        actual = _value(row, key)
        if isinstance(expected, dict):
            if "$exists" in expected:
                if (actual is not _MISSING) != bool(expected["$exists"]):
                    return False
            if "$nin" in expected and actual in expected["$nin"]:
                return False
            if "$in" in expected and actual not in expected["$in"]:
                return False
            continue
        if actual is _MISSING or actual != expected:
            return False
    return True


class _Result:
    def __init__(self, matched: int, modified: int):
        self.matched_count = matched
        self.modified_count = modified


class _Cursor:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.index = 0

    def sort(self, key, direction=1):
        keys = key if isinstance(key, list) else [(key, direction)]
        for field, order in reversed(keys):
            self.rows.sort(
                key=lambda row: (str(_value(row, field))), reverse=order < 0
            )
        return self

    def __aiter__(self):
        self.index = 0
        return self

    async def __anext__(self):
        if self.index >= len(self.rows):
            raise StopAsyncIteration
        row = self.rows[self.index]
        self.index += 1
        return copy.deepcopy(row)

    async def to_list(self, _limit):
        return copy.deepcopy(self.rows)


class _Collection:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.before_update = None
        self.update_calls = 0

    async def count_documents(self, query):
        return sum(_matches(row, query) for row in self.rows)

    def find(self, query, projection=None):
        rows = [copy.deepcopy(row) for row in self.rows if _matches(row, query)]
        if projection:
            included = {key for key, enabled in projection.items() if enabled}
            rows = [
                {key: value for key, value in row.items()
                 if key in included or key == "_id"}
                for row in rows
            ]
        return _Cursor(rows)

    async def find_one(self, query, projection=None, sort=None):
        cursor = self.find(query, projection)
        if sort:
            cursor.sort(sort)
        return await cursor.__anext__() if cursor.rows else None

    async def update_one(self, query, update):
        self.update_calls += 1
        if self.before_update:
            self.before_update(self, query, update)
        for row in self.rows:
            if not _matches(row, query):
                continue
            before = copy.deepcopy(row)
            for field, value in update.get("$set", {}).items():
                row[field] = copy.deepcopy(value)
            for field in update.get("$unset", {}):
                row.pop(field, None)
            return _Result(1, int(row != before))
        return _Result(0, 0)


class _DB:
    def __init__(self, *, documents: list[dict], parents: list[dict]):
        self.collections = {
            "documents": _Collection(copy.deepcopy(documents)),
            "parent_chunks": _Collection(copy.deepcopy(parents)),
            "chunks": _Collection([]),
            "corpora": _Collection([]),
        }

    def __getitem__(self, name):
        return self.collections[name]


def _fixture_db() -> _DB:
    documents = [
        {
            "doc_id": "d1", "corpus_id": "c1",
            "filename": "Jane Doe - Lighting Design (2020).pdf",
            "title": "Jane Doe   Lighting Design (2020)",
            "routing_trace": {"parser": "local_text", "title_source": "filename"},
        },
        {
            "doc_id": "d2", "corpus_id": "c1",
            "filename": "plain-notes.md", "title": "plain notes",
            "routing_trace": {"parser": "local_markdown", "title_source": "filename"},
        },
    ]
    parents = [
        {"corpus_id": "c1", "doc_id": "d1", "parent_id": "p1",
         "parent_index": 0, "text": "# Lighting Design"},
        {"corpus_id": "c1", "doc_id": "d2", "parent_id": "p2",
         "parent_index": 0, "text": "# Plain Notes"},
    ]
    return _DB(documents=documents, parents=parents)


def _biblio_snapshot(db: _DB):
    fields = {
        "author", "title", "language", "document_date", "source_published_at",
        "date_confidence", "bibliographic_provenance",
    }
    return [
        {key: copy.deepcopy(value) for key, value in row.items() if key in fields}
        for row in db["documents"].rows
    ]


@pytest.mark.asyncio
async def test_apply_writes_complete_backup_before_first_update_and_noop_is_lazy(tmp_path):
    db = _fixture_db()
    expected_rows = len(db["documents"].rows)

    def assert_backup_is_complete(_collection, _query, _update):
        paths = list(tmp_path.glob("*.jsonl"))
        assert len(paths) == 1
        assert sum(1 for _ in paths[0].open()) == expected_rows

    db["documents"].before_update = assert_backup_is_complete
    report = await run_corpus(
        db, "c1", "fixture", apply=True, force=False, head_chars=600,
        backup_dir=tmp_path, limit=None,
    )
    assert report["planned"] == expected_rows
    assert report["applied"] == expected_rows
    assert report["modified"] == expected_rows
    assert not report["aborted"]
    backup = Path(report["backup"])
    digest_before = hashlib.sha256(backup.read_bytes()).hexdigest()
    assert digest_before == report["backup_sha256"]
    assert report["backup_rows"] == expected_rows

    db["documents"].before_update = None
    rerun = await run_corpus(
        db, "c1", "fixture", apply=True, force=False, head_chars=600,
        backup_dir=tmp_path, limit=None,
    )
    assert rerun["planned"] == 0
    assert rerun["backup"] is None
    assert list(tmp_path.glob("*.jsonl")) == [backup]
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == digest_before


@pytest.mark.asyncio
async def test_restore_is_dry_by_default_and_restores_field_presence(tmp_path):
    db = _fixture_db()
    before = _biblio_snapshot(db)
    report = await run_corpus(
        db, "c1", "fixture", apply=True, force=False, head_chars=600,
        backup_dir=tmp_path, limit=None,
    )
    applied = _biblio_snapshot(db)
    assert applied != before

    dry = await restore_backup(db, Path(report["backup"]), apply=False)
    assert dry["planned"] == 2
    assert dry["restored"] == 0
    assert _biblio_snapshot(db) == applied

    restored = await restore_backup(db, Path(report["backup"]), apply=True)
    assert restored["restored"] == 2
    assert not restored["aborted"]
    assert _biblio_snapshot(db) == before


@pytest.mark.asyncio
async def test_cas_conflict_aborts_without_overwriting_concurrent_metadata(tmp_path):
    db = _fixture_db()

    def concurrent_write(collection, _query, _update):
        collection.before_update = None
        collection.rows[0]["author"] = "Concurrent Author"

    db["documents"].before_update = concurrent_write
    report = await run_corpus(
        db, "c1", "fixture", apply=True, force=False, head_chars=600,
        backup_dir=tmp_path, limit=1,
    )
    assert report["cas_conflicts"] == 1
    assert report["applied"] == 0
    assert report["aborted"]
    assert db["documents"].rows[0]["author"] == "Concurrent Author"
    assert "bibliographic_provenance" not in db["documents"].rows[0]
    assert Path(report["backup"]).exists()


def test_plan_unsets_legacy_file_date_family_even_after_unsafe_v1_stamp():
    doc = {
        "doc_id": "d", "corpus_id": "c", "filename": "scan.pdf",
        "title": "scan", "document_date": "2024-03-01",
        "source_published_at": "2024-03-01", "date_confidence": "low",
        "bibliographic_provenance": {
            "origin": "backfill_v1", "method": "pdf_creation_date",
            "reason": "file_date_only",
        },
        "routing_trace": {"parser": "pypdf_fast_text"},
    }
    plan = plan_for_document(
        doc, "", captured_at="2026-07-13T00:00:00+00:00", run_id="r1"
    )
    assert set(plan["unset_fields"]) == {
        "document_date", "source_published_at", "date_confidence",
    }
    assert plan["set_fields"]["bibliographic_provenance"]["reason"] \
        == "file_date_only"


def test_plan_supersedes_ingest_no_date_stamp_with_deterministic_date():
    doc = {
        "doc_id": "d", "corpus_id": "c", "filename": "paper-2020.md",
        "title": "paper 2020",
        "bibliographic_provenance": {
            "origin": "ingest", "method": "none", "reason": "no_date_source",
            "captured_at": "2026-07-12T00:00:00+00:00",
        },
        "routing_trace": {"parser": "local_markdown", "title_source": "filename"},
    }
    plan = plan_for_document(
        doc, "", captured_at="2026-07-13T00:00:00+00:00", run_id="r2"
    )
    assert plan["set_fields"]["document_date"] == "2020-01-01"
    assert plan["set_fields"]["source_published_at"] == "2020-01-01"
    assert plan["set_fields"]["date_confidence"] == "low"
    provenance = plan["set_fields"]["bibliographic_provenance"]
    assert provenance["method"] == "filename_year"
    assert provenance["prior"]["reason"] == "no_date_source"
    assert provenance["backfill"]["origin"] == BACKFILL_ORIGIN

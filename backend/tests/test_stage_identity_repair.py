from datetime import datetime

import pytest

from services.ingestion.extraction_jobs import extraction_contract_hash
from services.ingestion.stage_identity_repair import (
    backfill_ghost_b_stage_identity,
    build_ghost_b_stage_identity_update,
)


def _value_at(row, dotted):
    current = row
    for part in str(dotted).split("."):
        current = current.get(part) if isinstance(current, dict) else None
    return current


def _set_path(row, dotted, value):
    target = row
    parts = str(dotted).split(".")
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


def _matches(row, query):
    for key, expected in (query or {}).items():
        if key == "$or":
            if not any(_matches(row, item) for item in expected):
                return False
            continue
        if key == "$and":
            if not all(_matches(row, item) for item in expected):
                return False
            continue
        actual = _value_at(row, key)
        if isinstance(expected, dict):
            if "$exists" in expected:
                exists = actual is not None
                if bool(expected["$exists"]) != exists:
                    return False
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$nin" in expected and actual in expected["$nin"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
            continue
        if actual != expected:
            return False
    return True


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self._limit = len(self.rows)

    def limit(self, value):
        self._limit = value
        return self

    async def to_list(self, length=None):
        limit = self._limit if length is None else min(self._limit, length)
        return [dict(row) for row in self.rows[:limit]]


class _Collection:
    def __init__(self, rows=None):
        self.rows = [dict(row) for row in (rows or [])]

    def find(self, query=None, projection=None):
        rows = [row for row in self.rows if _matches(row, query or {})]
        if projection:
            projected = []
            for row in rows:
                out = {}
                for key, include in projection.items():
                    if not include:
                        continue
                    value = _value_at(row, key)
                    if value is not None:
                        _set_path(out, key, value)
                projected.append(out)
            rows = projected
        return _Cursor(rows)

    async def bulk_write(self, ops, ordered=False):
        del ordered
        modified = 0
        for op in ops:
            for idx, row in enumerate(self.rows):
                if not _matches(row, op._filter):
                    continue
                patched = dict(row)
                for key, value in (op._doc.get("$set") or {}).items():
                    _set_path(patched, key, value)
                self.rows[idx] = patched
                modified += 1
                break
        return type("Result", (), {"modified_count": modified})()


class _Db(dict):
    def __init__(self, **collections):
        super().__init__({name: _Collection(rows) for name, rows in collections.items()})

    def __getitem__(self, name):
        return self.setdefault(name, _Collection())


def test_build_ghost_b_stage_identity_update_uses_live_doc_and_chunk_identity():
    doc = {
        "doc_id": "doc-1",
        "updated_at": "doc-v1",
        "source_identity": {"content_sha256": "source-hash", "source_key": "sha256:source"},
        "ingestion_config": {
            "extraction_engine": "cloud",
            "use_neo4j": True,
            "extraction_models": [{"provider_preset": "vllm-rtx", "model": "m"}],
        },
    }
    chunk = {
        "chunk_id": "chunk-1",
        "text": "Alpha relates to beta.",
        "chunk_hash": "chunk-hash",
        "chunk_version": "chunk-v1",
    }
    update = build_ghost_b_stage_identity_update(
        {
            "corpus_id": "corpus-1",
            "doc_id": "doc-1",
            "chunk_id": "chunk-1",
            "status": "ok",
            "provider": "rtx",
        },
        doc=doc,
        chunk=chunk,
        contract_hash=extraction_contract_hash(doc),
        now=datetime.utcnow(),
    )

    assert update["chunk_hash"] == "chunk-hash"
    assert update["stage_identity"]["source_file_hash"] == "source-hash"
    assert update["stage_identity"]["source_key"] == "sha256:source"
    assert update["stage_identity"]["chunk_hash"] == "chunk-hash"
    assert update["stage_identity"]["chunk_version"] == "chunk-v1"
    assert update["stage_identity"]["doc_version"] == "doc-v1"
    assert update["stage_identity"]["extraction_contract_hash"] == update["extraction_contract_hash"]
    assert update["raw_output_artifact_id"].startswith("derived:")


@pytest.mark.asyncio
async def test_backfill_ghost_b_stage_identity_is_bounded_and_skips_missing_chunks():
    doc = {
        "corpus_id": "corpus-1",
        "doc_id": "doc-1",
        "source_identity": {"content_sha256": "source-hash"},
        "ingestion_config": {"extraction_engine": "cloud", "extraction_models": []},
    }
    db = _Db(
        documents=[doc],
        chunks=[
            {
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-ok",
                "text": "Live text.",
                "chunk_hash": "live-hash",
            }
        ],
        ghost_b_extractions=[
            {
                "_id": "row-ok",
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-ok",
                "status": "ok",
            },
            {
                "_id": "row-missing",
                "corpus_id": "corpus-1",
                "doc_id": "doc-1",
                "chunk_id": "chunk-missing",
                "status": "error",
            },
        ],
    )

    dry = await backfill_ghost_b_stage_identity(
        db,
        corpus_id="corpus-1",
        apply=False,
        limit=10,
    )
    assert dry["status"] == "planned"
    assert dry["planned"] == 1
    assert dry["modified"] == 0
    assert dry["skipped_missing_chunk"] == 1
    assert "stage_identity" not in db["ghost_b_extractions"].rows[0]

    applied = await backfill_ghost_b_stage_identity(
        db,
        corpus_id="corpus-1",
        apply=True,
        limit=10,
    )
    assert applied["status"] == "complete"
    assert applied["planned"] == 1
    assert applied["modified"] == 1
    row_ok = db["ghost_b_extractions"].rows[0]
    row_missing = db["ghost_b_extractions"].rows[1]
    assert row_ok["stage_identity"]["chunk_hash"] == "live-hash"
    assert row_ok["raw_output_artifact_id"].startswith("derived:")
    assert "stage_identity" not in row_missing

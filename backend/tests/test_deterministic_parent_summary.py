"""Deterministic parent-summary product contract tests (deterministic_summary.v1).

Owner decision: required summaries are deterministic, evidence-bound, and
provider-free. These tests pin the identity, extractive-only, and typed-write
invariants of ``services/ingestion/deterministic_summary.py``.
"""

from __future__ import annotations

import pytest

from services.ingestion import deterministic_summary as ds
from services.ingestion.deterministic_summary import (
    DETERMINISTIC_SUMMARY_MODEL_STAMP,
    DETERMINISTIC_SUMMARY_SCHEMA_VERSION,
    build_deterministic_parent_summary,
    deterministic_summary_config_hash,
    deterministic_summary_id,
    parent_source_hash,
    render_parent_summary,
    representative_sentences,
    run_deterministic_parent_summaries,
    to_parent_summary_write,
)

PARENT_TEXT = (
    "Benesh notation is a system for recording human movement. "
    "It was published in 1955 by Rudolf and Joan Benesh. "
    "The notation uses a five-line stave similar to music notation. "
    "Symbols mark the position of each body part at fixed time intervals. "
    "Choreographers used the system to preserve ballet repertory."
)

PARENT_ROW = {
    "parent_id": "parent_0001",
    "doc_id": "doc-1",
    "corpus_id": "corpus-1",
    "chunk_kind": "body",
    "heading_path": ["Chapter 1", "Movement Notation"],
    "text": PARENT_TEXT,
    "source_child_ids": ["chunk-a", "chunk-b"],
}


def test_summary_id_is_a_canonical_hash_and_stable():
    source_hash = parent_source_hash(PARENT_TEXT)
    first = deterministic_summary_id(parent_id="parent_0001", source_hash=source_hash)
    second = deterministic_summary_id(parent_id="parent_0001", source_hash=source_hash)
    assert first == second
    assert first.startswith("detsum_")
    # Different source content or parent identity must change the id.
    assert first != deterministic_summary_id(
        parent_id="parent_0002", source_hash=source_hash
    )
    assert first != deterministic_summary_id(
        parent_id="parent_0001", source_hash=parent_source_hash(PARENT_TEXT + " x")
    )


def test_config_hash_changes_reissue_ids():
    source_hash = parent_source_hash(PARENT_TEXT)
    baseline_hash = deterministic_summary_config_hash()
    baseline_id = deterministic_summary_id(
        parent_id="parent_0001", source_hash=source_hash
    )
    mutated = dict(ds.DETERMINISTIC_SUMMARY_CONFIG)
    mutated["max_key_points"] = 99
    try:
        ds.DETERMINISTIC_SUMMARY_CONFIG = mutated
        assert deterministic_summary_config_hash() != baseline_hash
        assert deterministic_summary_id(
            parent_id="parent_0001", source_hash=source_hash
        ) != baseline_id
    finally:
        ds.DETERMINISTIC_SUMMARY_CONFIG = dict(mutated, max_key_points=4)


def test_build_is_extractive_only_and_deterministic():
    child_rows = [
        {"chunk_id": "chunk-a", "chunk_kind": "body"},
        {"chunk_id": "chunk-b", "chunk_kind": "table"},
    ]
    extraction_rows = [
        {"parent_id": "parent_0001", "status": "accepted", "subject": "Benesh notation"},
        {"parent_id": "parent_0001", "status": "rejected", "subject": "GhostEntity"},
        {"parent_id": "parent_0001", "status": "corroborated", "entity": "Joan Benesh"},
    ]
    built = build_deterministic_parent_summary(
        parent_row=PARENT_ROW,
        child_rows=child_rows,
        extraction_rows=extraction_rows,
    )

    assert built["schema_version"] == DETERMINISTIC_SUMMARY_SCHEMA_VERSION
    assert built["summary_id"].startswith("detsum_")
    assert built["content_inventory"] == {"body": 1, "table": 1}
    assert "GhostEntity" not in built["key_entities"]
    assert "Benesh notation" in built["key_entities"]
    assert built["evidence_child_ids"] == ["chunk-a", "chunk-b"]
    assert "deterministic" in built["quality_flags"]
    assert "extractive_only" in built["quality_flags"]

    # Every key point must be a verbatim sentence from the parent text.
    sentences = set(ds.split_sentences(PARENT_TEXT))
    assert built["key_points"]
    for point in built["key_points"]:
        assert point in sentences

    # Determinism: identical inputs yield byte-identical artifacts.
    rebuilt = build_deterministic_parent_summary(
        parent_row=PARENT_ROW,
        child_rows=child_rows,
        extraction_rows=extraction_rows,
    )
    assert rebuilt == built


def test_render_is_a_bounded_function_of_fields():
    text = render_parent_summary(
        topic="Movement Notation",
        inventory={"body": 2},
        key_points=["Point one."],
        entities=["Benesh notation"],
        quality_flags=["deterministic", "extractive_only"],
    )
    assert text.startswith("Topic: Movement Notation.")
    assert "Content: 2 body." in text
    assert "Key entities: Benesh notation." in text
    assert len(text) <= int(ds.DETERMINISTIC_SUMMARY_CONFIG["max_summary_chars"])


def test_representative_sentences_are_position_ordered():
    picked = representative_sentences(
        PARENT_TEXT, heading_tokens=frozenset({"notation", "movement"})
    )
    sentences = ds.split_sentences(PARENT_TEXT)
    positions = [sentences.index(sentence) for sentence in picked]
    assert positions == sorted(positions)
    assert len(picked) <= int(ds.DETERMINISTIC_SUMMARY_CONFIG["max_key_points"])


def test_to_parent_summary_write_satisfies_typed_boundary():
    built = build_deterministic_parent_summary(parent_row=PARENT_ROW)
    write = to_parent_summary_write(built, parent_row=PARENT_ROW)

    record = write.record
    assert record.schema_version == DETERMINISTIC_SUMMARY_SCHEMA_VERSION
    assert record.summary_model == DETERMINISTIC_SUMMARY_MODEL_STAMP
    assert record.validation_status == "deterministic"
    assert record.abstraction_level == "evidence_bound"
    assert record.temporal_class == "unknown"
    assert record.time_expressions == []
    assert record.summary == built["summary"]
    assert write.parent_id == "parent_0001"
    assert write.source_text == PARENT_TEXT


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self._limit = len(rows)

    def sort(self, *_args):
        return self

    def limit(self, value):
        self._limit = value
        return self

    async def to_list(self, length=None):
        cap = self._limit if length is None else min(self._limit, length)
        return self._rows[:cap]

    def __aiter__(self):
        async def _gen():
            for row in self._rows:
                yield row

        return _gen()


class _Collection:
    def __init__(self, rows):
        self._rows = rows
        self.queries: list[dict] = []

    def find(self, query=None, *_args, **_kwargs):
        self.queries.append(query or {})
        return _Cursor(self._rows)


class _FakeDb:
    def __init__(self, parents, children=(), extractions=()):
        self.collections = {
            "parent_chunks": _Collection(parents),
            "chunks": _Collection(list(children)),
            "ghost_b_extractions": _Collection(list(extractions)),
        }
        self.written: list = []

    def __getitem__(self, name):
        return self.collections[name]


@pytest.mark.asyncio
async def test_run_deterministic_parent_summaries_writes_typed_records(monkeypatch):
    db = _FakeDb(
        parents=[PARENT_ROW],
        children=[{"chunk_id": "chunk-a", "chunk_kind": "body"}],
        extractions=[
            {"parent_id": "parent_0001", "status": "accepted", "subject": "Benesh notation"}
        ],
    )

    async def fake_write(_db, writes):
        db.written.extend(writes)

    monkeypatch.setattr("services.storage.mongo_writer.write_parent_summaries", fake_write)

    result = await run_deterministic_parent_summaries(db, corpus_id="corpus-1", limit=10)

    assert result["status"] == "healthy"
    assert result["product"] == DETERMINISTIC_SUMMARY_SCHEMA_VERSION
    assert result["generated"] == 1
    assert result["generation_errors"] == []
    assert len(db.written) == 1
    assert db.written[0].record.summary_model == DETERMINISTIC_SUMMARY_MODEL_STAMP


@pytest.mark.asyncio
async def test_run_deterministic_parent_summaries_empty(monkeypatch):
    db = _FakeDb(parents=[])

    async def fake_write(_db, writes):  # pragma: no cover - never reached
        raise AssertionError("empty slice must not write")

    monkeypatch.setattr("services.storage.mongo_writer.write_parent_summaries", fake_write)

    result = await run_deterministic_parent_summaries(db, corpus_id="corpus-1")
    assert result["status"] == "empty"
    assert result["generated"] == 0

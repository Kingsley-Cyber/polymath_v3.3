from datetime import datetime, timezone

import pytest

from scripts.polymath_summary_backfill_scoped import _summary_plan
from services.ghost_a import SummaryResult
from services.ingestion.summary_backfill import (
    summary_index_text,
    summary_result_fields,
    summary_write_from_result,
)
from services.ingestion.summary_semantics import repair_parent_summary_row


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, field, direction):
        reverse = direction < 0
        self.rows.sort(key=lambda row: row.get(field) or "", reverse=reverse)
        return self

    def limit(self, limit):
        self.rows = self.rows[:limit]
        return self

    async def to_list(self, length):
        return self.rows[:length]


def _field_matches(row, field, expected):
    value = row.get(field)
    exists = field in row
    if isinstance(expected, dict):
        for op, operand in expected.items():
            if op == "$exists":
                if exists is not bool(operand):
                    return False
            elif op == "$in":
                if value not in operand:
                    return False
            elif op == "$nin":
                if value in operand:
                    return False
            else:
                raise AssertionError(f"unsupported operator {op}")
        return True
    return value == expected


def _matches(row, query):
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(row, clause) for clause in expected):
                return False
        elif key == "$or":
            if not any(_matches(row, clause) for clause in expected):
                return False
        elif key == "$nor":
            if any(_matches(row, clause) for clause in expected):
                return False
        elif not _field_matches(row, key, expected):
            return False
    return True


class _ParentChunks:
    def __init__(self, rows):
        self.rows = rows

    async def count_documents(self, query):
        return sum(1 for row in self.rows if _matches(row, query))

    def find(self, query, projection=None):
        rows = []
        for row in self.rows:
            if not _matches(row, query):
                continue
            if projection:
                rows.append({key: row.get(key) for key, include in projection.items() if include})
            else:
                rows.append(dict(row))
        return _Cursor(rows)

    def aggregate(self, _pipeline):
        return _Cursor([])


class _FakeDb:
    def __init__(self, rows):
        self.parent_chunks = _ParentChunks(rows)

    def __getitem__(self, name):
        return getattr(self, name)


@pytest.mark.asyncio
async def test_scoped_summary_plan_uses_retrieval_parent_contract():
    db = _FakeDb(
        [
            {"corpus_id": "c1", "parent_id": "body-missing", "doc_id": "d1", "chunk_kind": "body"},
            {"corpus_id": "c1", "parent_id": "table-missing", "doc_id": "d1", "chunk_kind": "table"},
            {"corpus_id": "c1", "parent_id": "legacy-missing", "doc_id": "d1"},
            {
                "corpus_id": "c1",
                "parent_id": "body-done",
                "doc_id": "d2",
                "chunk_kind": "body",
                "summary": "done",
            },
            {
                "corpus_id": "c1",
                "parent_id": "table-done",
                "doc_id": "d2",
                "chunk_kind": "table",
                "summary": "done",
            },
            {"corpus_id": "c1", "parent_id": "toc-missing", "doc_id": "d3", "chunk_kind": "toc"},
            {"corpus_id": "c1", "parent_id": "code-missing", "doc_id": "d3", "chunk_kind": "code"},
        ]
    )

    plan = await _summary_plan(db, corpus_id="c1", limit=10)

    assert plan["retrieval_parent_count"] == 5
    assert plan["body_parent_count"] == 2
    assert plan["with_summary_text"] == 2
    assert plan["missing_summary_text"] == 3
    assert plan["body_with_summary_text"] == 1
    assert plan["body_missing_summary_text"] == 1
    assert plan["non_retrieval_missing_summary_text"] == 2
    assert plan["non_body_missing_summary_text"] == 2
    assert plan["coverage"] == 0.4
    assert plan["planned_parent_count"] == 3
    assert {row["parent_id"] for row in plan["planned_parents_sample"]} == {
        "body-missing",
        "legacy-missing",
        "table-missing",
    }


def test_summary_result_fields_preserves_canonical_artifact_metadata():
    updated_at = datetime.now(timezone.utc)
    result = SummaryResult(
        parent_id="parent-1",
        doc_id="doc-1",
        corpus_id="corpus-1",
        source_tier="parent",
        summary="A validated provider summary.",
        source_child_ids=["chunk-1"],
        schema_version="parent_summary.v1",
        summary_model="openai/tencent/Hy3",
        validation_status="valid",
        summary_id="summary-1",
        source_hash="source-hash",
        retrieval_text="A validated provider summary.",
    )

    write = summary_write_from_result(
        result,
        source_text="Source parent text.",
        updated_at=updated_at,
    )
    fields = summary_result_fields(write)

    assert fields["summary_model"] == "openai/tencent/Hy3"
    assert fields["schema_version"] == "parent_summary.v1"
    assert fields["validation_status"] == "valid"
    assert fields["source_child_ids"] == ["chunk-1"]
    assert fields["summary_updated_at"] == updated_at


def test_summary_index_text_prefers_canonical_retrieval_artifact():
    row = {
        "summary": "Short prose.",
        "retrieval_text": "Central claim plus grounded key points.",
    }

    assert summary_index_text(row) == "Central claim plus grounded key points."


def test_summary_index_text_repairs_legacy_structured_artifact():
    row = {
        "parent_id": "parent-1",
        "doc_id": "doc-1",
        "corpus_id": "corpus-1",
        "summary": "Short prose.",
        "central_claim": "The canonical claim carries more retrieval detail.",
        "key_points": ["First grounded point.", "Second grounded point."],
        "source_child_ids": ["chunk-1"],
    }

    text = summary_index_text(row)

    assert text != row["summary"]
    assert text == repair_parent_summary_row(row)["retrieval_text"]
    assert "The canonical claim carries more retrieval detail." in text

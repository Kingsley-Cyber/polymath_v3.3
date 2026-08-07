"""Summary readiness baseline tests.

The readiness contract must be satisfiable by deterministic summaries alone
(``llm_summary_required_for_query_ready: false``). These tests pin:

1. readiness summary clauses accept deterministic parent-summary records;
2. blocked durable jobs self-heal once their precondition is satisfied, so
   readiness can converge without operator intervention.
"""

from __future__ import annotations

import pytest

from services.ingestion import summary_jobs as sj
from services.ingestion.readiness import (
    SUMMARY_TEXT_CLAUSE,
    RETRIEVAL_PARENT_SUMMARY_CLAUSE,
)
from services.ingestion.summary_jobs import (
    classify_document_summary_status,
    reevaluate_blocked_summary_jobs,
)


def _clause_matches(clause: dict, row: dict) -> bool:
    """Minimal evaluator for the $exists/$nin clause shapes readiness uses."""

    for field, condition in clause.items():
        value = row.get(field)
        if isinstance(condition, dict):
            if "$exists" in condition:
                exists = field in row
                if bool(condition["$exists"]) != exists:
                    return False
            if "$nin" in condition and value in condition["$nin"]:
                return False
        elif value != condition:
            return False
    return True


def test_deterministic_summary_satisfies_readiness_text_clause():
    deterministic_row = {
        "summary": (
            "Topic: Movement Notation. Content: 2 body. "
            "Quality flags: deterministic, extractive_only."
        ),
        "schema_version": "deterministic_summary.v1",
        "summary_model": "deterministic:v1",
    }
    assert _clause_matches(SUMMARY_TEXT_CLAUSE, deterministic_row)

    for broken in ({"summary": ""}, {"summary": None}, {}):
        assert not _clause_matches(SUMMARY_TEXT_CLAUSE, broken)


def test_readiness_parent_clause_is_the_classifier_predicate():
    """The readiness predicate and the job-planning predicate must stay the
    same clause object so deterministic coverage counts identically in both."""

    from services.ingestion.section_classifier import parent_summary_required_clause

    assert RETRIEVAL_PARENT_SUMMARY_CLAUSE == parent_summary_required_clause()


def test_document_job_classification_transitions():
    assert classify_document_summary_status(
        required_parent_count=5, summarized_parent_count=0
    ) == ("blocked_no_parent_summaries", "no_parent_summary_context")
    assert classify_document_summary_status(
        required_parent_count=5, summarized_parent_count=3
    ) == ("blocked_parent_summaries_incomplete", "parent_summaries_incomplete")
    assert classify_document_summary_status(
        required_parent_count=5, summarized_parent_count=5
    ) == ("queued", "missing_document_summary")


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self._limit = len(rows)

    def limit(self, value):
        self._limit = value
        return self

    async def to_list(self, length=None):
        cap = self._limit if length is None else min(self._limit, length)
        return self._rows[:cap]


class _SummaryJobs:
    def __init__(self, rows):
        self._rows = rows

    def find(self, query=None, *_args, **_kwargs):
        statuses = ((query or {}).get("status") or {}).get("$in") or []
        rows = [row for row in self._rows if row["status"] in statuses]
        return _Cursor(rows)


class _ParentChunks:
    def __init__(self, required, summarized):
        self.required = required
        self.summarized = summarized

    async def count_documents(self, query):
        return self.summarized if "summary" in str(query) else self.required

    async def find_one(self, query=None, projection=None):
        return {"text": "parent body text"}


class _Db:
    def __init__(self, jobs, required, summarized):
        self.collections = {
            "summary_jobs": _SummaryJobs(jobs),
            "parent_chunks": _ParentChunks(required, summarized),
        }
        self.upserts: list = []

    def __getitem__(self, name):
        return self.collections[name]


@pytest.mark.asyncio
async def test_blocked_document_job_requeues_when_parents_are_summarized(monkeypatch):
    jobs = [
        {
            "job_id": "summary_doc_aaa",
            "kind": "document_summary",
            "doc_id": "doc-1",
            "status": "blocked_no_parent_summaries",
        }
    ]
    db = _Db(jobs, required=3, summarized=3)

    async def fake_bulk(collection, ops):
        db.upserts.extend(ops)

    monkeypatch.setattr(sj, "bulk_upsert_durable_jobs", fake_bulk)

    result = await reevaluate_blocked_summary_jobs(db, corpus_id="corpus-1")

    assert result == {"reevaluated": 1, "requeued": 1, "still_blocked": 0}
    assert len(db.upserts) == 1
    update = db.upserts[0]._doc  # pymongo UpdateOne introspection
    assert update["$set"]["status"] == "queued"
    assert update["$set"]["reason"] == "missing_document_summary"
    assert update["$set"]["missing_parent_count"] == 0


@pytest.mark.asyncio
async def test_blocked_document_job_stays_blocked_without_parents(monkeypatch):
    jobs = [
        {
            "job_id": "summary_doc_bbb",
            "kind": "document_summary",
            "doc_id": "doc-1",
            "status": "blocked_no_parent_summaries",
        }
    ]
    db = _Db(jobs, required=3, summarized=0)

    async def fake_bulk(collection, ops):  # pragma: no cover - nothing to write
        raise AssertionError("still-blocked jobs must not be updated")

    monkeypatch.setattr(sj, "bulk_upsert_durable_jobs", fake_bulk)

    result = await reevaluate_blocked_summary_jobs(db, corpus_id="corpus-1")
    assert result == {"reevaluated": 1, "requeued": 0, "still_blocked": 1}

import pytest

from services.ingestion.summary_jobs import plan_summary_jobs
from services.ingestion.summary_vector_reconcile import (
    audit_parent_summary_vector_integrity,
)


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self._limit = None

    def limit(self, value):
        self._limit = value
        return self

    def sort(self, *_args, **_kwargs):
        return self

    async def to_list(self, length=None):
        limit = self._limit or length
        return self.rows[:limit] if limit else list(self.rows)


class _Collection:
    def __init__(self, rows=None, find_one_value=None):
        self.rows = list(rows or [])
        self.find_one_value = find_one_value

    def find(self, query, projection=None):
        rows = [row for row in self.rows if _matches(row, query)]
        if projection:
            rows = [
                {key: row.get(key) for key, enabled in projection.items() if enabled}
                for row in rows
            ]
        return _Cursor(rows)

    async def find_one(self, *_args, **_kwargs):
        return self.find_one_value

    async def count_documents(self, query):
        return len([row for row in self.rows if _matches(row, query)])


class _Db(dict):
    def __getitem__(self, item):
        return dict.__getitem__(self, item)


def _get(row, dotted):
    cur = row
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _matches(row, query):
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(row, clause) for clause in expected):
                return False
            continue
        if key == "$or":
            if not any(_matches(row, clause) for clause in expected):
                return False
            continue
        actual = _get(row, key)
        if isinstance(expected, dict):
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$nin" in expected and actual in expected["$nin"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
            if "$exists" in expected and (actual is not None) is not bool(expected["$exists"]):
                return False
            continue
        if actual != expected:
            return False
    return True


@pytest.mark.asyncio
async def test_summary_planner_reaches_tail_after_materialized_first_page():
    db = _Db(
        {
            "corpora": _Collection(
                find_one_value={
                    "corpus_id": "c1",
                    "default_ingestion_config": {
                        "summary_models": [],
                        "chunk_summarization": True,
                    },
                }
            ),
            "summary_jobs": _Collection(
                [
                    {
                        "corpus_id": "c1",
                        "kind": "retrieval_parent_summary",
                        "parent_id": "p1",
                        "status": "queued",
                    },
                    {
                        "corpus_id": "c1",
                        "kind": "retrieval_parent_summary",
                        "parent_id": "p2",
                        "status": "running",
                    },
                ]
            ),
            "parent_chunks": _Collection(
                [
                    {
                        "corpus_id": "c1",
                        "doc_id": "d1",
                        "parent_id": "p1",
                        "text": "alpha",
                        "summary": "",
                    },
                    {
                        "corpus_id": "c1",
                        "doc_id": "d1",
                        "parent_id": "p2",
                        "text": "beta",
                        "summary": "",
                    },
                    {
                        "corpus_id": "c1",
                        "doc_id": "d1",
                        "parent_id": "p3",
                        "text": "gamma",
                        "summary": "",
                    },
                ]
            ),
            "documents": _Collection(
                [
                    {
                        "corpus_id": "c1",
                        "doc_id": "d1",
                        "user_id": "u1",
                        "ingest_stage": "queryable",
                    }
                ]
            ),
        }
    )

    result = await plan_summary_jobs(
        db,
        corpus_id="c1",
        user_id="u1",
        apply=False,
        limit=1,
        kinds=["retrieval_parent_summary"],
    )

    assert result["planned"] == 1
    assert result["jobs"][0]["parent_id"] == "p3"


class _Point:
    def __init__(self, payload):
        self.payload = payload


class _Qdrant:
    def __init__(self, parent_ids):
        self.parent_ids = list(parent_ids)

    async def scroll(self, **_kwargs):
        return (
            [
                _Point(
                    {
                        "corpus_id": "c1",
                        "chunk_type": "summary",
                        "parent_id": parent_id,
                    }
                )
                for parent_id in self.parent_ids
            ],
            None,
        )


@pytest.mark.asyncio
async def test_summary_vector_audit_is_parent_id_join_not_count_match():
    db = _Db(
        {
            "corpora": _Collection(
                find_one_value={
                    "default_ingestion_config": {
                        "target_qdrant_collections": ["hrag"],
                    }
                }
            ),
            "parent_chunks": _Collection(
                [
                    {
                        "corpus_id": "c1",
                        "parent_id": "p1",
                        "chunk_kind": "body",
                        "summary": "one",
                    },
                    {
                        "corpus_id": "c1",
                        "parent_id": "p2",
                        "chunk_kind": "body",
                        "summary": "two",
                    },
                ]
            ),
        }
    )

    result = await audit_parent_summary_vector_integrity(
        db,
        _Qdrant(["p1", "p3"]),
        corpus_id="c1",
        target_kinds=["hrag"],
    )

    assert result["required_mongo_ids"] == 2
    assert result["collections"]["hrag"]["qdrant_indexed_ids"] == 2
    assert result["collections"]["hrag"]["missing_ids"] == 1
    assert result["collections"]["hrag"]["missing_sample"] == ["p2"]

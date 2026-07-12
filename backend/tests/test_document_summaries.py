import asyncio

import pytest

from models.schemas import IngestionConfig
from services.ingestion import document_summaries
from services.ingestion import tier0


class _FindCursor:
    def __init__(self, rows):
        self.rows = rows
        self._limit = len(rows)

    def limit(self, value):
        self._limit = value
        return self

    async def to_list(self, length=None):
        limit = self._limit if length is None else min(self._limit, length)
        return self.rows[:limit]


class _Documents:
    def __init__(self, rows):
        self.rows = rows
        self.last_query = None

    def find(self, query=None, *_args, **_kwargs):
        self.last_query = query or {}
        return _FindCursor(self.rows)


class _Parents:
    def __init__(self, required, summarized):
        self.required = required
        self.summarized = summarized

    async def count_documents(self, query):
        return self.summarized if "summary" in str(query) else self.required


class _Db:
    def __init__(self, *, docs, required=2, summarized=2):
        self.collections = {
            "documents": _Documents(docs),
            "parent_chunks": _Parents(required, summarized),
        }

    def __getitem__(self, name):
        return self.collections[name]


@pytest.mark.asyncio
async def test_document_summary_backfill_builds_missing_profiles(monkeypatch):
    calls = []

    async def fake_pool(*_args, **_kwargs):
        async def fake_llm(_prompt):
            return "summary"

        return fake_llm, {"source": "corpus", "models": ["summary-model"], "lanes": 1}, IngestionConfig()

    async def fake_build_tree(**kwargs):
        calls.append(kwargs["doc_id"])
        return {"document": 1, "section": 1, "parents_in": 2}

    monkeypatch.setattr(document_summaries, "_summary_tree_pool_for_corpus", fake_pool)
    monkeypatch.setattr(document_summaries, "build_and_store_tree", fake_build_tree)

    result = await document_summaries.backfill_document_summaries(
        _Db(docs=[{"doc_id": "doc-1"}]),
        corpus_id="corpus-1",
        user_id="user-1",
        limit=5,
    )

    assert result["status"] == "complete"
    assert result["attempted"] == 1
    assert result["built"] == 1
    assert result["skipped"] == 0
    assert calls == ["doc-1"]
    assert result["results"][0]["status"] == "built"


@pytest.mark.asyncio
async def test_document_summary_backfill_projects_completed_profiles_to_tier0(
    monkeypatch,
):
    async def fake_pool(*_args, **_kwargs):
        return None, {"source": "none", "models": [], "lanes": 0}, IngestionConfig()

    async def fake_build_tree(**_kwargs):
        return {"document": 1, "section": 1, "parents_in": 2}

    projected = []

    async def fake_embed_profiles(_db, client, *, corpus_id, doc_ids, dim, api_key=None):
        projected.append((client, corpus_id, doc_ids, dim, api_key))
        return {"requested": len(doc_ids), "embedded": len(doc_ids)}

    monkeypatch.setattr(document_summaries, "_summary_tree_pool_for_corpus", fake_pool)
    monkeypatch.setattr(document_summaries, "build_and_store_tree", fake_build_tree)
    monkeypatch.setattr(tier0, "embed_doc_profiles", fake_embed_profiles)
    qdrant = object()

    result = await document_summaries.backfill_document_summaries(
        _Db(docs=[{"doc_id": "doc-1"}]),
        corpus_id="corpus-1",
        qdrant_client=qdrant,
        user_id="user-1",
        limit=5,
    )

    assert result["status"] == "complete"
    assert result["tier0_projection"] == {
        "status": "complete",
        "requested": 1,
        "embedded": 1,
    }
    assert projected == [(qdrant, "corpus-1", ["doc-1"], 1024, None)]


@pytest.mark.asyncio
async def test_document_summary_backfill_skips_partial_parent_coverage_without_llm(monkeypatch):
    async def fake_pool(*_args, **_kwargs):
        return None, {"source": "none", "models": [], "lanes": 0}, IngestionConfig()

    async def fail_build_tree(**_kwargs):
        raise AssertionError("partial parent coverage without LLM must not build a doc profile")

    monkeypatch.setattr(document_summaries, "_summary_tree_pool_for_corpus", fake_pool)
    monkeypatch.setattr(document_summaries, "build_and_store_tree", fail_build_tree)

    result = await document_summaries.backfill_document_summaries(
        _Db(docs=[{"doc_id": "doc-1"}], required=3, summarized=2),
        corpus_id="corpus-1",
        user_id="user-1",
        limit=5,
    )

    assert result["status"] == "complete"
    assert result["attempted"] == 1
    assert result["built"] == 0
    assert result["skipped"] == 1
    assert result["results"][0]["status"] == "skipped_parent_summaries_incomplete"


@pytest.mark.asyncio
async def test_document_summary_backfill_explicit_doc_ids_repair_profile_only_drift(monkeypatch):
    calls = []

    async def fake_pool(*_args, **_kwargs):
        async def fake_llm(_prompt):
            return "summary"

        return fake_llm, {"source": "corpus", "models": ["summary-model"], "lanes": 1}, IngestionConfig()

    async def fake_build_tree(**kwargs):
        calls.append(kwargs["doc_id"])
        return {"document": 1, "section": 1, "parents_in": 2}

    db = _Db(
        docs=[
            {
                "doc_id": "doc-1",
                "doc_profile": {"summary": "Existing profile summary."},
            }
        ],
    )
    monkeypatch.setattr(document_summaries, "_summary_tree_pool_for_corpus", fake_pool)
    monkeypatch.setattr(document_summaries, "build_and_store_tree", fake_build_tree)

    result = await document_summaries.backfill_document_summaries(
        db,
        corpus_id="corpus-1",
        user_id="user-1",
        limit=5,
        doc_ids=["doc-1"],
    )

    assert "$or" not in db["documents"].last_query
    assert result["status"] == "complete"
    assert result["built"] == 1
    assert calls == ["doc-1"]


@pytest.mark.asyncio
async def test_document_summary_backfill_uses_bounded_provider_concurrency(monkeypatch):
    active = 0
    peak = 0

    async def fake_pool(*_args, **_kwargs):
        async def fake_llm(_prompt):
            return "summary"

        return (
            fake_llm,
            {
                "source": "corpus",
                "models": ["summary-model"],
                "lanes": 1,
                "max_concurrent": 3,
            },
            IngestionConfig(),
        )

    async def fake_build_tree(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"document": 1, "doc_id": kwargs["doc_id"]}

    monkeypatch.setattr(document_summaries, "_summary_tree_pool_for_corpus", fake_pool)
    monkeypatch.setattr(document_summaries, "build_and_store_tree", fake_build_tree)

    docs = [{"doc_id": f"doc-{index}"} for index in range(8)]
    result = await document_summaries.backfill_document_summaries(
        _Db(docs=docs),
        corpus_id="corpus-1",
        user_id="user-1",
        limit=8,
    )

    assert result["status"] == "complete"
    assert result["built"] == 8
    assert result["max_concurrent"] == 3
    assert peak == 3
    assert [row["doc_id"] for row in result["results"]] == [
        f"doc-{index}" for index in range(8)
    ]


@pytest.mark.asyncio
async def test_document_summary_backfill_reuses_existing_tree_without_llm(monkeypatch):
    async def fake_pool(*_args, **_kwargs):
        async def fail_llm(_prompt):
            raise AssertionError("existing tree repair must not call the LLM")

        return (
            fail_llm,
            {"source": "corpus", "models": ["summary-model"], "lanes": 1},
            IngestionConfig(),
        )

    async def fake_sync(**kwargs):
        return {
            "status": "synced",
            "doc_id": kwargs["doc_id"],
            "node_id": "doc-1:document",
        }

    async def fail_build_tree(**_kwargs):
        raise AssertionError("existing tree repair must not rebuild the hierarchy")

    monkeypatch.setattr(document_summaries, "_summary_tree_pool_for_corpus", fake_pool)
    monkeypatch.setattr(
        document_summaries,
        "sync_document_profile_from_existing_tree",
        fake_sync,
    )
    monkeypatch.setattr(document_summaries, "build_and_store_tree", fail_build_tree)

    result = await document_summaries.backfill_document_summaries(
        _Db(docs=[{"doc_id": "doc-1"}]),
        corpus_id="corpus-1",
        user_id="user-1",
        limit=5,
    )

    assert result["built"] == 1
    assert (
        result["results"][0]["result"]["profile_source"]
        == "existing_summary_tree"
    )

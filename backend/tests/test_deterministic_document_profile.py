"""Deterministic document-profile tests.

The deterministic document lane builds profiles from the parent summaries that
exist; partial parent coverage is tolerated (the parent lane completes it on
later cycles) and no provider pool is ever resolved.
"""

from __future__ import annotations

import pytest

from services.ingestion import document_summaries


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

    def find(self, query=None, *_args, **_kwargs):
        return _FindCursor(self.rows)


class _Parents:
    def __init__(self, required, summarized):
        self.required = required
        self.summarized = summarized

    async def count_documents(self, query):
        return self.summarized if "summary" in str(query) else self.required


class _Corpora:
    async def find_one(self, query=None, projection=None):
        return {"default_ingestion_config": {}}


class _Db:
    def __init__(self, *, docs, required, summarized):
        self.collections = {
            "documents": _Documents(docs),
            "parent_chunks": _Parents(required, summarized),
            "corpora": _Corpora(),
        }

    def __getitem__(self, name):
        return self.collections[name]


def _patch(monkeypatch, build_calls):
    async def no_pool(*_args, **_kwargs):
        raise AssertionError("deterministic lane must not resolve a provider pool")

    async def no_sync(**_kwargs):
        return {"status": "no_tree"}

    async def fake_build_tree(**kwargs):
        build_calls.append(kwargs)
        return {"document": 1, "section": 1}

    monkeypatch.setattr(document_summaries, "_summary_tree_pool_for_corpus", no_pool)
    monkeypatch.setattr(
        document_summaries, "sync_document_profile_from_existing_tree", no_sync
    )
    monkeypatch.setattr(document_summaries, "build_and_store_tree", fake_build_tree)


@pytest.mark.asyncio
async def test_deterministic_profile_builds_with_partial_parent_coverage(monkeypatch):
    """4 required parents, only 2 summarized: the deterministic lane still
    builds (extractive over available summaries) instead of skipping."""

    build_calls: list[dict] = []
    _patch(monkeypatch, build_calls)

    result = await document_summaries.backfill_document_summaries(
        _Db(docs=[{"doc_id": "doc-1"}], required=4, summarized=2),
        corpus_id="corpus-1",
        user_id="user-1",
        limit=5,
        deterministic_only=True,
    )

    assert result["status"] == "complete"
    assert result["built"] == 1
    assert result["skipped"] == 0
    assert len(build_calls) == 1
    assert build_calls[0]["use_llm"] is False


@pytest.mark.asyncio
async def test_provider_lane_still_requires_complete_parents(monkeypatch):
    """Same partial coverage on a provider lane without an LLM keeps the
    historical skip-with-can-heal contract (behavior unchanged)."""

    build_calls: list[dict] = []

    async def llmless_pool(*_args, **_kwargs):
        from models.schemas import IngestionConfig

        return None, {"source": "resolved_flash_primary", "models": [], "lanes": 0, "max_concurrent": 1}, IngestionConfig()

    async def no_sync(**_kwargs):
        return {"status": "no_tree"}

    async def fake_build_tree(**kwargs):
        build_calls.append(kwargs)
        return {"document": 1}

    monkeypatch.setattr(document_summaries, "_summary_tree_pool_for_corpus", llmless_pool)
    monkeypatch.setattr(
        document_summaries, "sync_document_profile_from_existing_tree", no_sync
    )
    monkeypatch.setattr(document_summaries, "build_and_store_tree", fake_build_tree)

    result = await document_summaries.backfill_document_summaries(
        _Db(docs=[{"doc_id": "doc-1"}], required=4, summarized=2),
        corpus_id="corpus-1",
        user_id="user-1",
        limit=5,
        deterministic_only=False,
    )

    assert result["built"] == 0
    assert result["skipped"] == 1
    statuses = [row["status"] for row in result["results"]]
    assert statuses == ["skipped_parent_summaries_incomplete"]
    assert build_calls == []


@pytest.mark.asyncio
async def test_deterministic_profile_requires_at_least_one_parent_summary(monkeypatch):
    build_calls: list[dict] = []
    _patch(monkeypatch, build_calls)

    result = await document_summaries.backfill_document_summaries(
        _Db(docs=[{"doc_id": "doc-1"}], required=3, summarized=0),
        corpus_id="corpus-1",
        user_id="user-1",
        limit=5,
        deterministic_only=True,
    )

    assert result["built"] == 0
    statuses = [row["status"] for row in result["results"]]
    assert statuses == ["skipped_no_parent_summaries"]
    assert build_calls == []

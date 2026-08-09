"""Deterministic summary-tree lane tests.

The required document lane must build summary trees extractively
(``use_llm=False``) without any LLM function, provider pool, or cost
authority. ``build_and_store_tree`` only raises ``SummaryCostAuthorityRequired``
when a provider build is requested with no injected LLM.
"""

from __future__ import annotations

import pytest

from models.schemas import IngestionConfig
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
    def __init__(self, *, docs, required=2, summarized=2):
        self.collections = {
            "documents": _Documents(docs),
            "parent_chunks": _Parents(required, summarized),
            "corpora": _Corpora(),
        }

    def __getitem__(self, name):
        return self.collections[name]


@pytest.mark.asyncio
async def test_deterministic_lane_builds_tree_without_llm(monkeypatch):
    """deterministic_only=True must call build_and_store_tree with llm_fn=None
    and use_llm=False — the extractive tree path, no provider dispatch."""

    build_calls: list[dict] = []

    async def no_pool(*_args, **_kwargs):
        raise AssertionError("deterministic lane must not resolve a provider pool")

    async def no_sync(**_kwargs):
        return {"status": "no_tree"}

    async def fake_build_tree(**kwargs):
        build_calls.append(kwargs)
        return {"document": 1, "section": 1, "parents_in": 2}

    monkeypatch.setattr(
        document_summaries, "_summary_tree_pool_for_corpus", no_pool
    )
    monkeypatch.setattr(
        document_summaries, "sync_document_profile_from_existing_tree", no_sync
    )
    monkeypatch.setattr(document_summaries, "build_and_store_tree", fake_build_tree)

    result = await document_summaries.backfill_document_summaries(
        _Db(docs=[{"doc_id": "doc-1"}]),
        corpus_id="corpus-1",
        user_id="user-1",
        limit=5,
        deterministic_only=True,
    )

    assert result["status"] == "complete"
    assert result["built"] == 1
    contract = result["summary_contract"]
    assert contract["source"] == "deterministic_summary.v1"
    assert contract["models"] == []
    assert contract["lanes"] == 0
    assert len(build_calls) == 1
    call = build_calls[0]
    assert call["llm_fn"] is None
    assert call["use_llm"] is False
    assert call["heal_missing"] is False
    assert isinstance(call["embedding_config"], IngestionConfig)


@pytest.mark.asyncio
async def test_deterministic_tree_cost_errors_cannot_occur(monkeypatch):
    """SummaryCostAuthorityRequired is only raised for use_llm=True without an
    injected LLM. The deterministic contract never requests use_llm, so the
    gate is unreachable on the required path."""

    from services.ingestion.summary_cost_control import SummaryCostAuthorityRequired
    from services.ingestion.summary_tree import build_and_store_tree

    class _Docs:
        async def find_one(self, *_args, **_kwargs):
            return {"doc_id": "doc-1", "title": "t"}

    class _ParentCursor:
        def sort(self, *_args):
            return self

        async def to_list(self, length=None):
            return []

    class _ParentChunks:
        def find(self, *_args, **_kwargs):
            return _ParentCursor()

    class _Db2:
        def __getitem__(self, name):
            if name == "documents":
                return _Docs()
            if name == "parent_chunks":
                return _ParentChunks()
            raise KeyError(name)

    # use_llm=True with no llm_fn -> the cost-authority gate fires.
    with pytest.raises(SummaryCostAuthorityRequired):
        await build_and_store_tree(
            db=_Db2(), doc_id="doc-1", corpus_id="corpus-1", use_llm=True
        )

    # use_llm=False -> no gate; empty parents yield a clean skip.
    result = await build_and_store_tree(
        db=_Db2(), doc_id="doc-1", corpus_id="corpus-1", use_llm=False
    )
    assert result.get("skipped") == "no_parent_summaries"

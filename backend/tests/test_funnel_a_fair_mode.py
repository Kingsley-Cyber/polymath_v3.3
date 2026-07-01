"""Fair mode must BALANCE multi-corpus summaries, not black them out.

Before this fix, funnel_a.search() returned [] whenever fair_mode was on and
more than one corpus was selected ("Fair mode active for cross-corpus:
skipping Funnel A"). That threw away the broadest retrieval signal (parent
summaries) for exactly the queries that need breadth — cross-corpus ones —
to protect against a summary-heavy corpus dominating the pool.

The fix keeps the fairness guarantee but restores the signal: run the summary
search once per corpus with an equal per-corpus budget, so every corpus is
capped at its share and none can dominate.
"""

import os

import pytest

os.environ.setdefault("LITELLM_MASTER_KEY", "test-litellm-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

from models.schemas import SourceChunk
from services.retriever.funnel_a import FunnelA


def _corpus_scope(query_filter) -> list[str]:
    """Extract the corpus_id scope from a Qdrant filter built by funnel_a."""
    for cond in query_filter.must or []:
        if getattr(cond, "key", "") == "corpus_id":
            return list(cond.match.any)
    return []


def _chunk(cid: str, corpus: str, score: float) -> SourceChunk:
    return SourceChunk(
        chunk_id=cid,
        parent_id=f"p-{cid}",
        doc_id=f"doc-{cid}",
        corpus_id=corpus,
        text=f"summary {cid}",
        summary=f"summary {cid}",
        score=score,
        source_tier="summary",
    )


@pytest.fixture()
def funnel(monkeypatch):
    fa = FunnelA.__new__(FunnelA)  # skip real Qdrant client construction
    calls: list[list[str]] = []

    async def fake_search_collection(
        self, collection_name, query_vector, query_filter, limit, *, query_text=None
    ):
        scope = _corpus_scope(query_filter)
        calls.append(scope)
        # Corpus c1 is "summary-heavy": more and higher-scoring summaries.
        out = []
        for corpus in scope:
            n = 6 if corpus == "c1" else 3
            base = 0.9 if corpus == "c1" else 0.6
            out.extend(
                _chunk(f"{corpus}-{collection_name}-{i}", corpus, base - i * 0.01)
                for i in range(min(n, limit))
            )
        return out

    monkeypatch.setattr(FunnelA, "_search_collection", fake_search_collection)
    return fa, calls


@pytest.mark.asyncio
async def test_fair_mode_multi_corpus_returns_balanced_summaries(funnel):
    fa, calls = funnel
    chunks = await fa.search(
        query_vector=[0.1],
        corpus_ids=["c1", "c2"],
        collections=["naive"],
        top_k=8,
        fair_mode=True,
    )

    # The old behavior (blackout) returned [] here.
    assert chunks, "fair mode must not black out multi-corpus summaries"
    by_corpus: dict[str, int] = {}
    for c in chunks:
        by_corpus[c.corpus_id] = by_corpus.get(c.corpus_id, 0) + 1
    # Both corpora represented, and the summary-heavy corpus is capped at its
    # per-corpus share (top_k // n_corpora) — that's the fairness guarantee.
    assert set(by_corpus) == {"c1", "c2"}, by_corpus
    per_corpus_cap = max(2, 8 // 2)
    assert by_corpus["c1"] <= per_corpus_cap, by_corpus
    assert len(chunks) <= 8
    # Each per-corpus search was scoped to exactly one corpus.
    assert sorted(calls) == [["c1"], ["c2"]], calls


@pytest.mark.asyncio
async def test_fair_mode_single_corpus_unchanged(funnel):
    fa, calls = funnel
    chunks = await fa.search(
        query_vector=[0.1],
        corpus_ids=["c1"],
        collections=["naive"],
        top_k=5,
        fair_mode=True,
    )
    assert chunks
    assert calls == [["c1"]]
    assert len(chunks) <= 5


@pytest.mark.asyncio
async def test_fair_mode_off_keeps_single_global_search(funnel):
    fa, calls = funnel
    chunks = await fa.search(
        query_vector=[0.1],
        corpus_ids=["c1", "c2"],
        collections=["naive"],
        top_k=8,
        fair_mode=False,
    )
    assert chunks
    # One global search scoped to BOTH corpora — legacy unfair behavior kept
    # for callers that explicitly disable fair mode (BROAD intent).
    assert calls == [["c1", "c2"]], calls

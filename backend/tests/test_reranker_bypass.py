"""Reranker code-bypass tests.

The bypass splits the candidate pool by `chunk_kind` (detected via `language`
field), routes prose through the cross-encoder, keeps code chunks at their
pre-rerank scores, and merges with min-max normalization so neither side
crowds the other out.

Tests mock the HTTP sidecar so they don't depend on the reranker container
being up at test time.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.reranker import (
    RerankerService,
    _is_code_chunk,
    _minmax_inplace,
    _ranked_chunks_from_response,
)
from models.schemas import SourceChunk


def _chunk(text="t", score=0.5, language=None, chunk_id=None):
    return SourceChunk(
        chunk_id=chunk_id or f"c_{id(text)}",
        parent_id="p",
        doc_id="d",
        corpus_id="cor",
        text=text,
        score=score,
        source_tier="tier_code" if language else "tier_a",
        language=language,
    )


# ─── _is_code_chunk ─────────────────────────────────────────────────────────

def test_is_code_chunk_true_when_language_set():
    assert _is_code_chunk(_chunk(language="python"))
    assert _is_code_chunk(_chunk(language="luau"))


def test_is_code_chunk_false_for_prose():
    assert not _is_code_chunk(_chunk())
    assert not _is_code_chunk(_chunk(language=""))


# ─── _minmax_inplace ────────────────────────────────────────────────────────

def test_minmax_inplace_normalizes_range():
    pool = [_chunk(score=1.0), _chunk(score=5.0), _chunk(score=9.0)]
    _minmax_inplace(pool)
    assert pool[0].score == 0.0
    assert pool[1].score == 0.5
    assert pool[2].score == 1.0


def test_minmax_inplace_all_equal():
    pool = [_chunk(score=0.7), _chunk(score=0.7)]
    _minmax_inplace(pool)
    for c in pool:
        assert c.score == 1.0


def test_minmax_inplace_empty():
    _minmax_inplace([])  # must not raise


def test_minmax_inplace_single_element():
    pool = [_chunk(score=0.42)]
    _minmax_inplace(pool)
    assert pool[0].score == 1.0  # single → collapsed to top


# ─── Sidecar response shape adapters ───────────────────────────────────────

def test_ranked_chunks_from_results_response():
    pool = [_chunk(chunk_id="a"), _chunk(chunk_id="b")]
    out = _ranked_chunks_from_response(
        pool,
        {"results": [{"index": 1, "score": 7.5}, {"index": 0, "score": -2.0}]},
    )

    assert [c.chunk_id for c in out] == ["b", "a"]
    assert [c.score for c in out] == [7.5, -2.0]


def test_ranked_chunks_from_llamacpp_relevance_score_response():
    pool = [_chunk(chunk_id="a"), _chunk(chunk_id="b")]
    out = _ranked_chunks_from_response(
        pool,
        {
            "results": [
                {"index": 1, "relevance_score": 8.25},
                {"index": 0, "relevance_score": -1.5},
            ]
        },
    )

    assert [c.chunk_id for c in out] == ["b", "a"]
    assert [c.score for c in out] == [8.25, -1.5]


def test_ranked_chunks_from_openai_style_data_response():
    pool = [_chunk(chunk_id="a"), _chunk(chunk_id="b")]
    out = _ranked_chunks_from_response(
        pool,
        {
            "data": [
                {"document_index": 0, "score": 2.25},
                {"document_index": 1, "score": -3.0},
            ]
        },
    )

    assert [c.chunk_id for c in out] == ["a", "b"]
    assert [c.score for c in out] == [2.25, -3.0]


def test_ranked_chunks_from_scores_response_sorts_for_mlx_shape():
    pool = [_chunk(chunk_id="a"), _chunk(chunk_id="b"), _chunk(chunk_id="c")]
    out = _ranked_chunks_from_response(pool, {"scores": [0.2, 0.9, 0.1]})

    assert [c.chunk_id for c in out] == ["b", "a", "c"]
    assert [c.score for c in out] == [0.9, 0.2, 0.1]


# ─── RerankerService.rerank ─────────────────────────────────────────────────

@pytest.fixture
def svc(monkeypatch):
    """A RerankerService whose settings expose RERANKER_BYPASS_CODE=True."""
    s = RerankerService()
    s._settings = SimpleNamespace(
        RERANKER_URL="http://reranker:8080",
        RERANKER_BYPASS_CODE=True,
    )
    return s


@pytest.mark.asyncio
async def test_rerank_empty_returns_empty(svc):
    out = await svc.rerank("hello", [])
    assert out == []


@pytest.mark.asyncio
async def test_rerank_all_code_skips_cross_encoder(svc, monkeypatch):
    """All-code pool: don't even call the sidecar."""
    pool_call_count = {"n": 0}

    async def fake_rerank_pool(*args, **kwargs):
        pool_call_count["n"] += 1
        return []

    monkeypatch.setattr(svc, "_rerank_pool", fake_rerank_pool)
    pool = [_chunk(score=0.3, language="luau"), _chunk(score=0.7, language="luau")]
    out = await svc.rerank("how do I tween", pool)
    assert len(out) == 2
    assert out[0].score == 0.7  # original order preserved
    assert pool_call_count["n"] == 0  # cross-encoder never called
    assert svc.diagnostics()["status"] == "bypassed_all_code"


@pytest.mark.asyncio
async def test_rerank_all_prose_uses_cross_encoder(svc, monkeypatch):
    """All-prose pool: behaves like the legacy path."""
    async def fake_rerank_pool(query, pool):
        # Reverse scores: pretend the cross-encoder swapped the ordering
        for i, c in enumerate(pool):
            c.score = float(len(pool) - i)
        return pool

    monkeypatch.setattr(svc, "_rerank_pool", fake_rerank_pool)
    pool = [_chunk(score=0.3), _chunk(score=0.7)]
    out = await svc.rerank("explain animation", pool)
    assert len(out) == 2
    assert out[0].score == 2.0  # cross-encoder gave first chunk highest score


@pytest.mark.asyncio
async def test_rerank_mixed_pool_partitions(svc, monkeypatch):
    """Mixed pool: prose gets reranked, code keeps pre-rerank scores,
    both normalized and merged. Verify call count and merge shape."""
    rerank_call_args = {}

    async def fake_rerank_pool(query, pool):
        rerank_call_args["query"] = query
        rerank_call_args["pool"] = pool
        # Reranker gives prose chunks score = 8.0
        for c in pool:
            c.score = 8.0
        return pool

    monkeypatch.setattr(svc, "_rerank_pool", fake_rerank_pool)

    pool = [
        _chunk(text="prose1", score=0.3, chunk_id="p1"),
        _chunk(text="prose2", score=0.5, chunk_id="p2"),
        _chunk(text="code1", score=0.6, language="luau", chunk_id="c1"),
        _chunk(text="code2", score=0.9, language="luau", chunk_id="c2"),
    ]
    out = await svc.rerank("how do I tween", pool)

    assert len(out) == 4
    # Cross-encoder was called ONCE, on only the prose subpool
    assert len(rerank_call_args["pool"]) == 2
    assert all(not _is_code_chunk(c) for c in rerank_call_args["pool"])

    # After min-max normalization, each pool's top entry has score=1.0
    # so the final order interleaves: code-top, prose-top, code-bottom, prose-bottom
    by_id = {c.chunk_id: c for c in out}
    assert by_id["c1"].score < by_id["c2"].score  # original code order preserved
    assert by_id["p1"].score <= by_id["p2"].score  # prose ordering came from rerank
    # All scores in [0, 1]
    assert all(0.0 <= c.score <= 1.0 for c in out)


@pytest.mark.asyncio
async def test_rerank_mixed_tiny_pool_clamps_code_score_on_bounded_scale(svc, monkeypatch):
    """A single bypassed code chunk must not keep a raw lexical score like
    103 inside a probability/cosine rerank pool. That score later drives
    bounded tail trimming and can delete every useful prose chunk."""
    svc._settings = SimpleNamespace(
        RERANKER_URL="http://reranker:8080",
        RERANKER_BYPASS_CODE=True,
        RERANKER_SCORE_SCALE="probability",
    )

    async def fake_rerank_pool(query, pool):
        return [pool[0].model_copy(update={"score": 0.72})]

    monkeypatch.setattr(svc, "_rerank_pool", fake_rerank_pool)

    pool = [
        _chunk(
            text="Natural language processing is often prototyped in Python.",
            score=0.4,
            chunk_id="prose",
        ),
        _chunk(
            text="```python\nprint('mandelbrot')\n```",
            score=103.0,
            language="python",
            chunk_id="code",
        ),
    ]

    out = await svc.rerank("what is nlp and python", pool)
    by_id = {c.chunk_id: c for c in out}

    assert by_id["code"].score == 1.0
    assert by_id["prose"].score == 0.72
    assert all(0.0 <= c.score <= 1.0 for c in out)


@pytest.mark.asyncio
async def test_rerank_bypass_disabled_skips_partition(svc, monkeypatch):
    """When RERANKER_BYPASS_CODE=False, the legacy path runs (whole pool
    through cross-encoder, no partition)."""
    svc._settings = SimpleNamespace(
        RERANKER_URL="http://reranker:8080",
        RERANKER_BYPASS_CODE=False,
    )
    pool_sizes = []

    async def fake_rerank_pool(query, pool):
        pool_sizes.append(len(pool))
        return pool

    monkeypatch.setattr(svc, "_rerank_pool", fake_rerank_pool)
    pool = [_chunk(language="luau"), _chunk(language="luau"), _chunk()]
    await svc.rerank("query", pool)
    # Single call with the full mixed pool
    assert pool_sizes == [3]


@pytest.mark.asyncio
async def test_rerank_failure_opens_short_circuit(svc, monkeypatch):
    """One sidecar failure should not make the same chat turn wait on repeat
    HTTP attempts. The next call falls back locally while the circuit is open."""
    svc._settings = SimpleNamespace(
        RERANKER_URL="http://reranker:8080",
        RERANKER_BYPASS_CODE=False,
        RERANKER_TIMEOUT_SECONDS=0.5,
        RERANKER_CIRCUIT_BREAKER_SECONDS=60.0,
    )
    calls = {"client": 0}

    class FailingClient:
        def __init__(self, timeout):
            calls["client"] += 1
            assert timeout == 0.5

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            raise RuntimeError("reranker sick")

    monkeypatch.setattr("services.reranker.httpx.AsyncClient", FailingClient)

    pool = [_chunk(score=0.2, chunk_id="low"), _chunk(score=0.9, chunk_id="high")]
    first = await svc._rerank_pool("query", pool)
    second = await svc._rerank_pool("query", pool)

    assert calls["client"] == 1
    assert [c.chunk_id for c in first] == ["high", "low"]
    assert [c.chunk_id for c in second] == ["high", "low"]
    assert svc.diagnostics()["status"] == "circuit_open"
    assert svc.diagnostics()["fallback"] is True


@pytest.mark.asyncio
async def test_rerank_batch_failure_splits_and_preserves_successes(svc, monkeypatch):
    """A realistic sidecar can 500 on a batch while scoring smaller pieces.
    The client should split the batch instead of opening the global circuit."""
    svc._settings = SimpleNamespace(
        RERANKER_URL="http://reranker:8080",
        RERANKER_BYPASS_CODE=False,
        RERANKER_TIMEOUT_SECONDS=0.5,
        RERANKER_CIRCUIT_BREAKER_SECONDS=60.0,
    )
    calls: list[list[str]] = []

    async def fake_post_batch(*, client, url, query, pool):
        calls.append([c.chunk_id for c in pool])
        if len(pool) > 1:
            raise RuntimeError("batch too large")
        chunk = pool[0].model_copy()
        chunk.score = {"a": 0.1, "b": 0.8, "c": 0.3, "d": 0.6}[chunk.chunk_id]
        return [chunk]

    monkeypatch.setattr(svc, "_post_rerank_batch", fake_post_batch)

    pool = [
        _chunk(score=0.1, chunk_id="a"),
        _chunk(score=0.2, chunk_id="b"),
        _chunk(score=0.3, chunk_id="c"),
        _chunk(score=0.4, chunk_id="d"),
    ]
    out = await svc._rerank_pool("query", pool)

    assert [c.chunk_id for c in out] == ["b", "d", "c", "a"]
    assert calls[0] == ["a", "b", "c", "d"]
    assert ["a"] in calls and ["b"] in calls and ["c"] in calls and ["d"] in calls
    assert svc._disabled_until == 0.0
    assert svc.diagnostics()["status"] == "used"
    assert svc.diagnostics()["successes"] == 4
    assert svc.diagnostics()["failures"] == 0


@pytest.mark.asyncio
async def test_rerank_partial_failure_budget_opens_circuit(svc, monkeypatch):
    """If a sidecar partially recovers but keeps 500ing on candidates, keep
    the current mixed result and short-circuit follow-up retrieval passes."""
    svc._settings = SimpleNamespace(
        RERANKER_URL="http://reranker:8080",
        RERANKER_BYPASS_CODE=False,
        RERANKER_TIMEOUT_SECONDS=0.5,
        RERANKER_CIRCUIT_BREAKER_SECONDS=60.0,
    )
    monkeypatch.setattr("services.reranker._RERANK_PARTIAL_FAILURE_BUDGET", 2)
    calls: list[list[str]] = []

    async def fake_post_batch(*, client, url, query, pool):
        calls.append([c.chunk_id for c in pool])
        if len(pool) > 1:
            raise RuntimeError("batch too large")
        if pool[0].chunk_id in {"c", "d"}:
            raise RuntimeError("candidate breaks sidecar")
        chunk = pool[0].model_copy()
        chunk.score = {"a": 0.4, "b": 0.9}[chunk.chunk_id]
        return [chunk]

    monkeypatch.setattr(svc, "_post_rerank_batch", fake_post_batch)

    pool = [
        _chunk(score=0.1, chunk_id="a"),
        _chunk(score=0.2, chunk_id="b"),
        _chunk(score=0.8, chunk_id="c"),
        _chunk(score=0.7, chunk_id="d"),
    ]
    first = await svc._rerank_pool("query", pool)
    second = await svc._rerank_pool("query", pool)

    assert [c.chunk_id for c in first] == ["b", "c", "d", "a"]
    assert [c.chunk_id for c in second] == ["c", "d", "b", "a"]
    assert svc._disabled_until > 0.0
    assert calls.count(["a", "b", "c", "d"]) == 1


@pytest.mark.asyncio
async def test_rerank_default_bypass_is_on():
    """Verify the setting default is True so the bypass is opt-OUT, not opt-in."""
    from config import get_settings
    s = get_settings()
    assert s.RERANKER_BYPASS_CODE is True

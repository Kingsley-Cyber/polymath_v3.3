"""Phase 5a — metrics-aware graph_rerank tests.

Pins the contract for `apply_graph_degree_boost_metrics_aware`:

  • db=None or cache-empty → behaves like the existing degree-only
    `apply_graph_degree_boost`. Same scores, same multipliers.
  • Cache present + entity in top_pagerank → PageRank-derived
    pseudo-degree contributes; final multiplier = MAX(degree-derived,
    pagerank-derived).
  • Cache present + entity NOT in top_pagerank → that entity gets
    no PageRank contribution; degree-only multiplier applies.
  • Mixed corpora (some warm, some cold) → merged top_pagerank
    lookup across the warm ones; cold corpora contribute nothing.
  • Cypher failure → chunks returned unchanged (no crash, no score
    mutation).
  • Cache lookup failure → degree-only multiplier (no crash).
  • Empty chunks / no neo4j → no-op.

Uses the same fake-driver pattern from prior phase tests; no live
Neo4j or Qdrant required.
"""
from __future__ import annotations

import math
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Auth-package stubs ───────────────────────────────────────────────
def _install_stubs_if_missing() -> None:
    if "jose" not in sys.modules:
        try:
            import jose  # noqa: F401
        except ImportError:
            jose_mod = ModuleType("jose")

            class JWTError(Exception):
                pass

            class _Jwt:
                @staticmethod
                def encode(*_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

                @staticmethod
                def decode(*_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

            jose_mod.JWTError = JWTError
            jose_mod.jwt = _Jwt()
            sys.modules["jose"] = jose_mod

    if "passlib.context" not in sys.modules:
        try:
            import passlib.context  # noqa: F401
        except ImportError:
            passlib_mod = ModuleType("passlib")
            ctx_mod = ModuleType("passlib.context")

            class _CryptContext:
                def __init__(self, *a, **kw):
                    pass

                def hash(self, *_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

                def verify(self, *_a, **_kw):  # pragma: no cover
                    raise RuntimeError("stub")

            ctx_mod.CryptContext = _CryptContext
            passlib_mod.context = ctx_mod
            sys.modules["passlib"] = passlib_mod
            sys.modules["passlib.context"] = ctx_mod

    if "slowapi" not in sys.modules:
        try:
            import slowapi  # noqa: F401
        except ImportError:
            slowapi_mod = ModuleType("slowapi")
            util_mod = ModuleType("slowapi.util")

            class _Limiter:
                def __init__(self, *a, **kw):
                    pass

                def limit(self, *_a, **_kw):
                    def _decorator(fn):
                        return fn

                    return _decorator

            def _get_remote_address(_request):  # pragma: no cover
                return "0.0.0.0"

            slowapi_mod.Limiter = _Limiter
            util_mod.get_remote_address = _get_remote_address
            sys.modules["slowapi"] = slowapi_mod
            sys.modules["slowapi.util"] = util_mod


_install_stubs_if_missing()


from services.retriever import graph_rerank  # noqa: E402


# ── Fake Neo4j driver returning canned chunk→entity rows ────────────


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def __aiter__(self):
        return self._async_iter()

    async def _async_iter(self):
        for r in self._rows:
            yield r


class _FakeSession:
    def __init__(self, on_run):
        self._on_run = on_run

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def run(self, *args, **kwargs):
        return self._on_run(*args, **kwargs)


class _FakeDriver:
    def __init__(self, on_run):
        self._on_run = on_run

    def session(self):
        return _FakeSession(self._on_run)


def _build_cypher_driver(rows):
    """Driver that returns the same rows for every session.run call."""

    def on_run(cypher, **kwargs):
        corpora_by_chunk = {
            str(ref["chunk_id"]): str(ref["corpus_id"])
            for ref in kwargs.get("chunk_refs", [])
        }
        return _FakeResult(
            [
                {
                    **row,
                    "corpus_id": row.get("corpus_id")
                    or corpora_by_chunk.get(str(row.get("chunk_id") or ""), ""),
                }
                for row in rows
            ]
        )

    return _FakeDriver(on_run)


class _StubChunk:
    """SourceChunk shape sufficient for the rerank function."""

    def __init__(self, chunk_id: str, score: float):
        self.chunk_id = chunk_id
        self.corpus_id = "corp-1"
        self.score = score


# ── Tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_chunks_is_noop():
    """No chunks → returns the empty list, no Cypher call."""

    def on_run(*a, **k):  # pragma: no cover — should not be called
        raise AssertionError("Cypher should not run on empty chunks")

    driver = _FakeDriver(on_run)
    out = await graph_rerank.apply_graph_degree_boost_metrics_aware(
        chunks=[],
        corpus_ids=["corp-1"],
        neo4j_driver=driver,
        db=MagicMock(),
    )
    assert out == []


@pytest.mark.asyncio
async def test_no_neo4j_driver_is_noop():
    """neo4j_driver=None → returns chunks unchanged."""
    chunks = [_StubChunk("c1", 1.0)]
    out = await graph_rerank.apply_graph_degree_boost_metrics_aware(
        chunks=chunks,
        corpus_ids=["corp-1"],
        neo4j_driver=None,
        db=MagicMock(),
    )
    assert out[0].score == 1.0


@pytest.mark.asyncio
async def test_db_none_falls_back_to_degree_only():
    """db=None → cache lookup is skipped, multiplier comes from
    degree alone (identical to apply_graph_degree_boost)."""
    chunks = [_StubChunk("c1", 1.0)]
    rows = [{"chunk_id": "c1", "entities": [{"entity_id": "e1", "degree": 10}]}]
    driver = _build_cypher_driver(rows)
    await graph_rerank.apply_graph_degree_boost_metrics_aware(
        chunks=chunks,
        corpus_ids=["corp-1"],
        neo4j_driver=driver,
        db=None,
    )
    # multiplier = 1 + 0.15 * log1p(min(10, 50)) = 1 + 0.15 * log1p(10)
    expected = 1.0 + 0.15 * math.log1p(10)
    assert chunks[0].score == pytest.approx(expected)


@pytest.mark.asyncio
async def test_metrics_aware_cypher_carries_entity_flags_across_with():
    """Regression: Neo4j drops variables not carried through WITH clauses."""
    chunks = [_StubChunk("c1", 1.0)]
    captured: dict[str, str] = {}

    def on_run(cypher, **kwargs):
        captured["cypher"] = cypher
        return _FakeResult(
            [{"chunk_id": "c1", "entities": [{"entity_id": "e1", "degree": 1}]}]
        )

    driver = _FakeDriver(on_run)
    await graph_rerank.apply_graph_degree_boost_metrics_aware(
        chunks=chunks,
        corpus_ids=["corp-1"],
        neo4j_driver=driver,
        db=None,
    )

    cypher = captured["cypher"]
    assert "AS generic_entity" in cypher
    assert "AS graph_expansion_allowed" in cypher
    assert "WHEN generic_entity" in cypher
    assert "WHEN coalesce(e.generic_entity" not in cypher


@pytest.mark.asyncio
async def test_cold_cache_no_metrics_row_falls_back_to_degree():
    """db present but get_cached_metrics returns None → degree-only
    multiplier (cache contribution is 0)."""
    chunks = [_StubChunk("c1", 1.0)]
    rows = [{"chunk_id": "c1", "entities": [{"entity_id": "e1", "degree": 10}]}]
    driver = _build_cypher_driver(rows)
    with (
        patch(
            "services.graph.analytics.compute_corpus_change_signature",
            new=AsyncMock(return_value="sig"),
        ),
        patch(
            "services.graph.analytics.get_cached_metrics",
            new=AsyncMock(return_value=None),
        ),
    ):
        await graph_rerank.apply_graph_degree_boost_metrics_aware(
            chunks=chunks,
            corpus_ids=["corp-1"],
            neo4j_driver=driver,
            db=MagicMock(),
        )
    expected = 1.0 + 0.15 * math.log1p(10)
    assert chunks[0].score == pytest.approx(expected)


@pytest.mark.asyncio
async def test_warm_cache_entity_in_top_k_uses_pagerank_signal():
    """Cache present + entity in top_pagerank with a HIGH pr score
    that pseudo-degrees ABOVE the local degree → PageRank signal
    wins, score multiplier larger than the degree-only baseline."""
    chunks = [_StubChunk("c1", 1.0)]
    # Entity has low local degree (3) but high PageRank (0.08).
    # pseudo_degree = 0.08 * 500 = 40, which beats degree=3.
    rows = [{"chunk_id": "c1", "entities": [{"entity_id": "e1", "degree": 3}]}]
    driver = _build_cypher_driver(rows)
    fake_metrics = SimpleNamespace(
        edge_count=42,  # non-zero so the sparse-graph guard doesn't fire
        top_pagerank=[{"entity_id": "e1", "score": 0.08}],
    )
    with (
        patch(
            "services.graph.analytics.compute_corpus_change_signature",
            new=AsyncMock(return_value="sig"),
        ),
        patch(
            "services.graph.analytics.get_cached_metrics",
            new=AsyncMock(return_value=fake_metrics),
        ),
    ):
        await graph_rerank.apply_graph_degree_boost_metrics_aware(
            chunks=chunks,
            corpus_ids=["corp-1"],
            neo4j_driver=driver,
            db=MagicMock(),
        )
    # Combined signal = max(min(3, 50), min(40, 50)) = 40.
    expected = 1.0 + 0.15 * math.log1p(40)
    assert chunks[0].score == pytest.approx(expected)
    # Sanity check: this is strictly LARGER than degree-only would have been.
    degree_only = 1.0 + 0.15 * math.log1p(3)
    assert chunks[0].score > degree_only


@pytest.mark.asyncio
async def test_warm_cache_entity_not_in_top_k_uses_degree():
    """Cache present but entity NOT in top_pagerank → that entity
    contributes only its local degree, identical to cold-cache."""
    chunks = [_StubChunk("c1", 1.0)]
    rows = [{"chunk_id": "c1", "entities": [{"entity_id": "off-list", "degree": 10}]}]
    driver = _build_cypher_driver(rows)
    fake_metrics = SimpleNamespace(
        top_pagerank=[{"entity_id": "different-entity", "score": 0.99}],
    )
    with (
        patch(
            "services.graph.analytics.compute_corpus_change_signature",
            new=AsyncMock(return_value="sig"),
        ),
        patch(
            "services.graph.analytics.get_cached_metrics",
            new=AsyncMock(return_value=fake_metrics),
        ),
    ):
        await graph_rerank.apply_graph_degree_boost_metrics_aware(
            chunks=chunks,
            corpus_ids=["corp-1"],
            neo4j_driver=driver,
            db=MagicMock(),
        )
    expected = 1.0 + 0.15 * math.log1p(10)
    assert chunks[0].score == pytest.approx(expected)


@pytest.mark.asyncio
async def test_degree_wins_when_higher_than_pagerank_pseudo():
    """Even with PageRank cache present, if the local degree pseudo
    is HIGHER, the MAX picks degree. The cache layer never reduces
    the multiplier — it only contributes additional signal."""
    chunks = [_StubChunk("c1", 1.0)]
    # Degree=45 (close to cap) vs PR=0.02 → pseudo_degree=10. Degree wins.
    rows = [{"chunk_id": "c1", "entities": [{"entity_id": "e1", "degree": 45}]}]
    driver = _build_cypher_driver(rows)
    fake_metrics = SimpleNamespace(
        top_pagerank=[{"entity_id": "e1", "score": 0.02}],
    )
    with (
        patch(
            "services.graph.analytics.compute_corpus_change_signature",
            new=AsyncMock(return_value="sig"),
        ),
        patch(
            "services.graph.analytics.get_cached_metrics",
            new=AsyncMock(return_value=fake_metrics),
        ),
    ):
        await graph_rerank.apply_graph_degree_boost_metrics_aware(
            chunks=chunks,
            corpus_ids=["corp-1"],
            neo4j_driver=driver,
            db=MagicMock(),
        )
    expected = 1.0 + 0.15 * math.log1p(45)
    assert chunks[0].score == pytest.approx(expected)


@pytest.mark.asyncio
async def test_max_degree_cap_clips_outlier_pagerank():
    """A PageRank score that would pseudo-degree above MAX_DEGREE_CAP
    is clipped — same as the cap on raw degree. Prevents one super-
    high-PR entity from dominating the multiplier."""
    chunks = [_StubChunk("c1", 1.0)]
    rows = [{"chunk_id": "c1", "entities": [{"entity_id": "e1", "degree": 0}]}]
    driver = _build_cypher_driver(rows)
    # PR=0.5 → pseudo_degree=250 → capped to MAX_DEGREE_CAP=50.
    fake_metrics = SimpleNamespace(
        edge_count=42,  # non-zero — sparse-graph guard inactive
        top_pagerank=[{"entity_id": "e1", "score": 0.5}],
    )
    with (
        patch(
            "services.graph.analytics.compute_corpus_change_signature",
            new=AsyncMock(return_value="sig"),
        ),
        patch(
            "services.graph.analytics.get_cached_metrics",
            new=AsyncMock(return_value=fake_metrics),
        ),
    ):
        await graph_rerank.apply_graph_degree_boost_metrics_aware(
            chunks=chunks,
            corpus_ids=["corp-1"],
            neo4j_driver=driver,
            db=MagicMock(),
        )
    expected = 1.0 + 0.15 * math.log1p(graph_rerank.MAX_DEGREE_CAP)
    assert chunks[0].score == pytest.approx(expected)


@pytest.mark.asyncio
async def test_cache_lookup_failure_falls_back_to_degree():
    """get_cached_metrics raises → caught, logged, degree-only path
    keeps working. No exception escapes."""
    chunks = [_StubChunk("c1", 1.0)]
    rows = [{"chunk_id": "c1", "entities": [{"entity_id": "e1", "degree": 8}]}]
    driver = _build_cypher_driver(rows)
    with (
        patch(
            "services.graph.analytics.compute_corpus_change_signature",
            new=AsyncMock(side_effect=RuntimeError("mongo down")),
        ),
        patch(
            "services.graph.analytics.get_cached_metrics",
            new=AsyncMock(return_value=None),
        ),
    ):
        await graph_rerank.apply_graph_degree_boost_metrics_aware(
            chunks=chunks,
            corpus_ids=["corp-1"],
            neo4j_driver=driver,
            db=MagicMock(),
        )
    expected = 1.0 + 0.15 * math.log1p(8)
    assert chunks[0].score == pytest.approx(expected)


@pytest.mark.asyncio
async def test_cypher_failure_returns_chunks_unchanged():
    """Cypher round-trip raises → return chunks unchanged. No score
    mutation, no exception. Same fail-closed contract as the existing
    apply_graph_degree_boost."""
    chunks = [_StubChunk("c1", 1.0)]

    def on_run(*a, **k):
        raise RuntimeError("neo4j connection refused")

    driver = _FakeDriver(on_run)
    out = await graph_rerank.apply_graph_degree_boost_metrics_aware(
        chunks=chunks,
        corpus_ids=["corp-1"],
        neo4j_driver=driver,
        db=MagicMock(),
    )
    assert out[0].score == 1.0  # unchanged


@pytest.mark.asyncio
async def test_multi_corpus_merges_top_pagerank_lookups():
    """Multiple corpus_ids with warm caches → top_pagerank entries
    merge (dedup by entity_id; higher score wins on overlap)."""
    chunks = [_StubChunk("c1", 1.0)]
    rows = [{"chunk_id": "c1", "entities": [{"entity_id": "e_shared", "degree": 2}]}]
    driver = _build_cypher_driver(rows)
    # Two corpora; e_shared appears in both with different scores.
    metrics_a = SimpleNamespace(
        edge_count=20,
        top_pagerank=[{"entity_id": "e_shared", "score": 0.03}],
    )
    metrics_b = SimpleNamespace(
        edge_count=35,
        top_pagerank=[{"entity_id": "e_shared", "score": 0.09}],  # higher
    )
    metrics_by_corpus = {"corp-A": metrics_a, "corp-B": metrics_b}

    async def fake_get(_db, corpus_id, _sig):
        return metrics_by_corpus.get(corpus_id)

    with (
        patch(
            "services.graph.analytics.compute_corpus_change_signature",
            new=AsyncMock(return_value="sig"),
        ),
        patch("services.graph.analytics.get_cached_metrics", new=fake_get),
    ):
        await graph_rerank.apply_graph_degree_boost_metrics_aware(
            chunks=chunks,
            corpus_ids=["corp-A", "corp-B"],
            neo4j_driver=driver,
            db=MagicMock(),
        )
    # The higher 0.09 score should win on the merge.
    # pseudo_degree = 0.09 * 500 = 45.
    expected = 1.0 + 0.15 * math.log1p(45)
    assert chunks[0].score == pytest.approx(expected)


@pytest.mark.asyncio
async def test_zero_degree_no_pagerank_skips_boost():
    """Entity with degree=0 AND no PageRank entry → combined signal=0
    → no multiplier applied, score unchanged."""
    chunks = [_StubChunk("c1", 1.0)]
    rows = [{"chunk_id": "c1", "entities": [{"entity_id": "e1", "degree": 0}]}]
    driver = _build_cypher_driver(rows)
    with (
        patch(
            "services.graph.analytics.compute_corpus_change_signature",
            new=AsyncMock(return_value="sig"),
        ),
        patch(
            "services.graph.analytics.get_cached_metrics",
            new=AsyncMock(return_value=None),
        ),
    ):
        await graph_rerank.apply_graph_degree_boost_metrics_aware(
            chunks=chunks,
            corpus_ids=["corp-1"],
            neo4j_driver=driver,
            db=MagicMock(),
        )
    assert chunks[0].score == 1.0  # unchanged


# ── Phase 5a follow-up — sparse-graph guard ─────────────────────────


@pytest.mark.asyncio
async def test_zero_edge_corpus_skips_pagerank_contribution():
    """The bug the debugging review caught: a 0-edge corpus produces
    a uniform NetworkX PageRank distribution (1/N per node). With
    PR=0.111 and _PR_TO_DEGREE_SCALE=500, the pseudo-degree is ~55,
    which caps to MAX_DEGREE_CAP=50 → multiplier ≈ 1.59. That would
    boost EVERY entity-mentioning chunk to maximum even though the
    graph has no structure.

    With the edge_count==0 guard the PR lookup is skipped for that
    corpus and the multiplier falls back to degree-only behavior
    (multiplier=1.0 when degree=0)."""
    chunks = [_StubChunk("c1", 1.0)]
    # Entity has 0 local degree (no RELATES_TO edges in the corpus).
    rows = [{"chunk_id": "c1", "entities": [{"entity_id": "e1", "degree": 0}]}]
    driver = _build_cypher_driver(rows)
    # Metrics with uniform PR (simulates a sparse / 0-edge corpus).
    # If the guard wasn't there, e1's PR of 0.111 would multiply by
    # 500 → 55 → cap to 50 → multiplier 1.59.
    fake_metrics = SimpleNamespace(
        edge_count=0,
        top_pagerank=[
            {"entity_id": "e1", "score": 0.111},
            {"entity_id": "e2", "score": 0.111},
        ],
    )
    with (
        patch(
            "services.graph.analytics.compute_corpus_change_signature",
            new=AsyncMock(return_value="sig"),
        ),
        patch(
            "services.graph.analytics.get_cached_metrics",
            new=AsyncMock(return_value=fake_metrics),
        ),
    ):
        await graph_rerank.apply_graph_degree_boost_metrics_aware(
            chunks=chunks,
            corpus_ids=["corp-sparse"],
            neo4j_driver=driver,
            db=MagicMock(),
        )
    # Guard fired → PR lookup skipped → degree-only → degree=0 → no
    # multiplier applied → score unchanged.
    assert chunks[0].score == 1.0


@pytest.mark.asyncio
async def test_multi_corpus_only_dense_corpus_contributes_pr():
    """Two-corpus query: one dense (edge_count > 0), one sparse
    (edge_count = 0). The sparse corpus's uniform PR must NOT pollute
    the merged pagerank_lookup. Only the dense corpus contributes."""
    chunks = [_StubChunk("c1", 1.0)]
    rows = [{"chunk_id": "c1", "entities": [{"entity_id": "e_in_dense", "degree": 5}]}]
    driver = _build_cypher_driver(rows)
    sparse_metrics = SimpleNamespace(
        edge_count=0,
        top_pagerank=[
            # If this leaked into the lookup it would dominate
            # (pseudo_degree = 0.111 * 500 ≈ 55 → caps to 50).
            {"entity_id": "e_in_dense", "score": 0.111},
        ],
    )
    dense_metrics = SimpleNamespace(
        edge_count=42,  # actual graph has edges
        top_pagerank=[
            # Modest real PR — pseudo_degree = 0.02 * 500 = 10.
            {"entity_id": "e_in_dense", "score": 0.02},
        ],
    )
    metrics_by_corpus = {"corp-sparse": sparse_metrics, "corp-dense": dense_metrics}

    async def fake_get(_db, corpus_id, _sig):
        return metrics_by_corpus.get(corpus_id)

    with (
        patch(
            "services.graph.analytics.compute_corpus_change_signature",
            new=AsyncMock(return_value="sig"),
        ),
        patch("services.graph.analytics.get_cached_metrics", new=fake_get),
    ):
        await graph_rerank.apply_graph_degree_boost_metrics_aware(
            chunks=chunks,
            corpus_ids=["corp-sparse", "corp-dense"],
            neo4j_driver=driver,
            db=MagicMock(),
        )
    # Combined signal = max(min(degree=5, 50), min(pseudo=10, 50)) = 10.
    # Multiplier = 1 + 0.15 * log1p(10).
    expected = 1.0 + 0.15 * math.log1p(10)
    assert chunks[0].score == pytest.approx(expected)
    # Sanity check: this is substantially LESS than the inflated
    # multiplier the sparse corpus would have produced if its PR
    # had been merged into the lookup (which would have given
    # pseudo_degree = min(55, 50) = 50, multiplier ≈ 1.59).
    inflated_if_buggy = 1.0 + 0.15 * math.log1p(graph_rerank.MAX_DEGREE_CAP)
    assert chunks[0].score < inflated_if_buggy


@pytest.mark.asyncio
async def test_zero_edge_corpus_still_uses_local_degree():
    """Even with the 0-edge guard, if a chunk's mentioned entity has
    a non-zero local degree (which can only happen if Neo4j has more
    edges than the cache recorded — schema-drift scenario), the
    degree path still applies its multiplier. The guard only skips
    the PR contribution; degree is independent."""
    chunks = [_StubChunk("c1", 1.0)]
    rows = [{"chunk_id": "c1", "entities": [{"entity_id": "e1", "degree": 8}]}]
    driver = _build_cypher_driver(rows)
    fake_metrics = SimpleNamespace(
        edge_count=0,  # cache says 0 edges
        top_pagerank=[{"entity_id": "e1", "score": 0.111}],  # uniform PR
    )
    with (
        patch(
            "services.graph.analytics.compute_corpus_change_signature",
            new=AsyncMock(return_value="sig"),
        ),
        patch(
            "services.graph.analytics.get_cached_metrics",
            new=AsyncMock(return_value=fake_metrics),
        ),
    ):
        await graph_rerank.apply_graph_degree_boost_metrics_aware(
            chunks=chunks,
            corpus_ids=["corp-1"],
            neo4j_driver=driver,
            db=MagicMock(),
        )
    # PR skipped, degree=8 contributes → multiplier = 1 + 0.15 * log1p(8).
    expected = 1.0 + 0.15 * math.log1p(8)
    assert chunks[0].score == pytest.approx(expected)

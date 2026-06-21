"""Phase 2 hybrid hub + bridge tests.

Pins the contract for the analytics-aware paths added to
`find_hubs` (top_pagerank) and `find_bridges` (entity_betweenness +
fragile_bridges):

  • metrics=None → exact pre-Phase-2 behavior (degree count / path
    count Cypher). Backwards-compat is non-negotiable for cold
    corpora.
  • metrics with relevant entries → elite path produces results
    ranked by structural importance, not local degree.
  • metrics with no overlap to current scope → graceful fallback to
    the Cypher path. The metrics cache is corpus-scoped; the query
    is sub-corpus-scoped, and we trust the fallback every time the
    overlap is empty.

Mocks the Neo4j driver (same fake-session pattern as the Phase 1
tests) so these run without a live cluster.
"""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


# ── Auth-package stubs (mirrors test_graph_query_hybrid.py) ──────────
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


from services.graph import graph_query  # noqa: E402


# ── Fake Neo4j driver (same as Phase 1 tests) ────────────────────────


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


# ── find_hubs tests ──────────────────────────────────────────────────


def _node(nid, name="", etype="Concept", is_seed=False):
    return {
        "id": nid,
        "display_name": name,
        "entity_type": etype,
        "is_seed": is_seed,
    }


def _link(s, t):
    return {"source": s, "target": t, "predicate": "related_to", "confidence": 0.5}


def test_find_hubs_degree_fallback_when_no_metrics():
    """metrics=None → exact pre-Phase-2 behavior (degree count)."""
    nodes = [_node("a", "A"), _node("b", "B"), _node("c", "C")]
    links = [_link("a", "b"), _link("a", "c"), _link("b", "c")]
    out = graph_query.find_hubs(nodes, links, top_n=3, metrics=None)
    assert len(out) == 3
    for row in out:
        assert row["source"] == "degree"
        assert "pagerank_score" not in row


def test_find_hubs_pagerank_when_metrics_warm_and_overlap():
    """metrics with top_pagerank that overlaps current subgraph → elite path."""
    nodes = [_node("a", "Alpha"), _node("b", "Beta"), _node("c", "Gamma")]
    links = [_link("a", "b"), _link("b", "c")]  # b has degree 2, a/c degree 1
    # Cache says 'a' (low local degree) has the highest PageRank globally.
    # Phase 2 elite path should rank 'a' first, NOT 'b'.
    metrics = SimpleNamespace(top_pagerank=[
        {"entity_id": "a", "canonical_name": "Alpha", "score": 0.42},
        {"entity_id": "c", "canonical_name": "Gamma", "score": 0.15},
        {"entity_id": "z_offscope", "canonical_name": "Z", "score": 0.99},
    ])
    out = graph_query.find_hubs(nodes, links, top_n=3, metrics=metrics)
    # z_offscope is in the cache but NOT in the subgraph → filtered out.
    ids = [r["entity_id"] for r in out]
    assert "z_offscope" not in ids
    # 'a' wins on PageRank even though 'b' wins on local degree.
    assert ids[0] == "a"
    assert out[0]["source"] == "pagerank"
    assert out[0]["pagerank_score"] == pytest.approx(0.42)
    # Local degree still carried for context.
    assert out[0]["degree"] == 1


def test_find_hubs_falls_back_when_no_overlap():
    """metrics with top_pagerank that has NO entities in the subgraph
    → falls back to degree count (don't return an empty list)."""
    nodes = [_node("a", "A"), _node("b", "B")]
    links = [_link("a", "b")]
    metrics = SimpleNamespace(top_pagerank=[
        {"entity_id": "z1", "score": 0.9},
        {"entity_id": "z2", "score": 0.8},
    ])
    out = graph_query.find_hubs(nodes, links, top_n=2, metrics=metrics)
    assert len(out) == 2
    assert all(r["source"] == "degree" for r in out)


def test_find_hubs_falls_back_when_top_pagerank_empty():
    """metrics with an empty top_pagerank list → fallback path."""
    nodes = [_node("a", "A"), _node("b", "B")]
    links = [_link("a", "b")]
    metrics = SimpleNamespace(top_pagerank=[])
    out = graph_query.find_hubs(nodes, links, top_n=2, metrics=metrics)
    assert all(r["source"] == "degree" for r in out)


# ── find_bridges tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_bridges_path_count_fallback_when_no_metrics():
    """metrics=None → original Cypher path-count behavior."""
    def on_run(cypher, **kwargs):
        return _FakeResult([
            {
                "entity_id": "b1",
                "display_name": "Bridge1",
                "entity_type": "Concept",
                "connected_seed_count": 2,
                "connected_seeds": ["s1", "s2"],
            }
        ])
    driver = _FakeDriver(on_run)
    out = await graph_query.find_bridges(
        driver=driver,
        entity_ids=["s1", "s2"],
        corpus_id="corp-1",
        metrics=None,
    )
    assert len(out) == 1
    assert out[0]["source"] == "path_count"
    assert out[0]["entity_id"] == "b1"


@pytest.mark.asyncio
async def test_find_bridges_path_count_uses_edge_pruning_policy():
    """Fallback bridge discovery must not count weak generic edges as bridges."""
    queries: list[str] = []

    def on_run(cypher, **kwargs):
        queries.append(cypher)
        assert kwargs["min_edge_confidence"] == pytest.approx(0.20)
        assert kwargs["generic_min_confidence"] == pytest.approx(0.35)
        assert "related_to" in kwargs["generic_predicates"]
        assert "strong" in kwargs["strong_edge_strengths"]
        return _FakeResult([])

    driver = _FakeDriver(on_run)
    out = await graph_query.find_bridges(
        driver=driver,
        entity_ids=["s1", "s2"],
        corpus_id="corp-1",
        metrics=None,
    )
    joined = "\n".join(queries)
    assert out == []
    assert "eligible_for_synthesis" in joined
    assert "edge_strength" in joined
    assert "evidence_count" in joined
    assert "$generic_min_confidence" in joined


@pytest.mark.asyncio
async def test_find_bridges_fragile_path_when_metrics_warm():
    """metrics with a fragile_bridge anchored to a seed → elite path
    emits the non-seed endpoint as a bridge."""
    def on_run(*a, **k):  # pragma: no cover — should not be called
        raise AssertionError("Cypher should not run when elite path succeeds")

    driver = _FakeDriver(on_run)
    metrics = SimpleNamespace(
        fragile_bridges=[
            {
                "source": "s1",          # seed
                "target": "x9",          # non-seed → this becomes the bridge
                "source_name": "Seed1",
                "target_name": "Cross-Domain Connector",
                "source_domain": "physics",
                "target_domain": "biology",
                "evidence": "articulation edge",
            }
        ],
        entity_betweenness={},
        top_pagerank=[],
    )
    out = await graph_query.find_bridges(
        driver=driver,
        entity_ids=["s1", "s2"],
        corpus_id="corp-1",
        metrics=metrics,
    )
    assert len(out) == 1
    r = out[0]
    assert r["source"] == "fragile"
    assert r["entity_id"] == "x9"
    assert r["display_name"] == "Cross-Domain Connector"
    assert r["fragile_partner"] == "s1"
    assert r["evidence"] == "articulation edge"


@pytest.mark.asyncio
async def test_find_bridges_betweenness_path():
    """metrics with entity_betweenness → high-centrality non-seed
    entities surface as bridges, ranked by score."""
    def on_run(*a, **k):  # pragma: no cover
        raise AssertionError("Cypher should not run")

    driver = _FakeDriver(on_run)
    metrics = SimpleNamespace(
        fragile_bridges=[],
        entity_betweenness={
            "x1": 0.85,   # high centrality
            "x2": 0.20,
            "s1": 0.99,   # seed → must be excluded
        },
        top_pagerank=[
            {"entity_id": "x1", "canonical_name": "Bottleneck1"},
            {"entity_id": "x2", "canonical_name": "Bottleneck2"},
        ],
    )
    out = await graph_query.find_bridges(
        driver=driver,
        entity_ids=["s1"],
        corpus_id="corp-1",
        metrics=metrics,
    )
    ids = [r["entity_id"] for r in out]
    assert "s1" not in ids  # seed filtered out
    assert ids[0] == "x1"   # highest betweenness wins
    assert out[0]["source"] == "betweenness"
    assert out[0]["betweenness"] == pytest.approx(0.85)
    # Names hydrated from top_pagerank lookup.
    assert out[0]["display_name"] == "Bottleneck1"


@pytest.mark.asyncio
async def test_find_bridges_mixed_fragile_first_then_betweenness():
    """When both signals fire, fragile bridges sort first
    (high-value, low-volume), betweenness second."""
    def on_run(*a, **k):  # pragma: no cover
        raise AssertionError("Cypher should not run")

    driver = _FakeDriver(on_run)
    metrics = SimpleNamespace(
        fragile_bridges=[
            {
                "source": "s1",
                "target": "x9",
                "source_name": "Seed1",
                "target_name": "FragileBridge",
                "source_domain": "a",
                "target_domain": "b",
                "evidence": "art-edge",
            }
        ],
        entity_betweenness={"x7": 0.9, "x8": 0.6},
        top_pagerank=[{"entity_id": "x7", "canonical_name": "X7"}],
    )
    out = await graph_query.find_bridges(
        driver=driver,
        entity_ids=["s1"],
        corpus_id="corp-1",
        metrics=metrics,
    )
    sources = [r["source"] for r in out]
    # All fragile entries before any betweenness entries.
    fragile_idx = [i for i, s in enumerate(sources) if s == "fragile"]
    between_idx = [i for i, s in enumerate(sources) if s == "betweenness"]
    assert fragile_idx
    assert between_idx
    assert max(fragile_idx) < min(between_idx)


@pytest.mark.asyncio
async def test_find_bridges_falls_back_when_metrics_have_no_seed_overlap():
    """Elite path produces zero bridges (no fragile entries touching
    seeds AND no betweenness entries) → Cypher path-count fallback
    still runs."""
    captured = {}

    def on_run(cypher, **kwargs):
        captured["called"] = True
        return _FakeResult([
            {
                "entity_id": "b1",
                "display_name": "PathCountBridge",
                "entity_type": "Concept",
                "connected_seed_count": 2,
                "connected_seeds": ["s1", "s2"],
            }
        ])

    driver = _FakeDriver(on_run)
    # Cache exists but doesn't reach this query's seeds at all.
    metrics = SimpleNamespace(
        fragile_bridges=[
            {
                "source": "z1",       # neither z1 nor z2 are seeds
                "target": "z2",
                "source_name": "Z1",
                "target_name": "Z2",
                "source_domain": "a",
                "target_domain": "b",
            }
        ],
        entity_betweenness={},
        top_pagerank=[],
    )
    out = await graph_query.find_bridges(
        driver=driver,
        entity_ids=["s1", "s2"],
        corpus_id="corp-1",
        metrics=metrics,
    )
    assert captured.get("called") is True
    assert len(out) == 1
    assert out[0]["source"] == "path_count"


@pytest.mark.asyncio
async def test_find_bridges_single_seed_no_fragile_returns_empty():
    """With <2 seeds AND no elite-path results, return empty (the
    Cypher path-count needs ≥2 seeds to be meaningful)."""
    def on_run(*a, **k):  # pragma: no cover
        raise AssertionError("Cypher should not run for <2-seed query")

    driver = _FakeDriver(on_run)
    metrics = SimpleNamespace(
        fragile_bridges=[],
        entity_betweenness={},
        top_pagerank=[],
    )
    out = await graph_query.find_bridges(
        driver=driver,
        entity_ids=["s1"],
        corpus_id="corp-1",
        metrics=metrics,
    )
    assert out == []


@pytest.mark.asyncio
async def test_find_bridges_single_seed_with_fragile_still_works():
    """Phase 2 unlock — a single-seed query can NOW produce bridges
    via fragile_bridges (the elite path doesn't require ≥2 seeds the
    way the path-count fallback does)."""
    def on_run(*a, **k):  # pragma: no cover
        raise AssertionError("Cypher should not run")

    driver = _FakeDriver(on_run)
    metrics = SimpleNamespace(
        fragile_bridges=[
            {
                "source": "s1",
                "target": "x9",
                "source_name": "Seed",
                "target_name": "CrossDomain",
                "source_domain": "a",
                "target_domain": "b",
            }
        ],
        entity_betweenness={},
        top_pagerank=[],
    )
    out = await graph_query.find_bridges(
        driver=driver,
        entity_ids=["s1"],  # only one seed!
        corpus_id="corp-1",
        metrics=metrics,
    )
    assert len(out) == 1
    assert out[0]["source"] == "fragile"

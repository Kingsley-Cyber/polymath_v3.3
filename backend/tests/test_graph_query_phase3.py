"""Phase 3 hybrid expand_subgraph + find_gaps tests.

Pins the contract for the analytics-aware paths added to
`expand_subgraph` (PageRank / concept_id / is_working_entity
annotation) and `find_gaps` (terminological + analogy + transfer
gap types layered on top of missing_edge).

Cold-cache fallback contract is non-negotiable:
  • metrics=None → exact pre-Phase-3 behavior, byte-for-byte.
  • metrics warm: additive annotation (expand_subgraph) and
    additive gap types (find_gaps). No data is lost.
  • If select_working_entities raises (half-deserialized cache),
    the annotation is skipped but the BFS rows still return.

Same fake-Neo4j-driver pattern as the Phase 1 + 2 tests.
"""
from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

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


from services.graph import graph_query  # noqa: E402


# ── Fake Neo4j driver ────────────────────────────────────────────────


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


def _build_expand_driver(node_rows, edge_rows):
    """Driver that returns node_rows on the first session.run() and
    edge_rows on the second (matches expand_subgraph's two-Cypher
    pattern)."""
    state = {"calls": 0}

    def on_run(cypher, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            return _FakeResult(node_rows)
        return _FakeResult(edge_rows)

    return _FakeDriver(on_run)


# ── expand_subgraph tests ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_expand_subgraph_cold_cache_unannotated():
    """metrics=None → no annotations added. Byte-for-byte
    pre-Phase-3 shape."""
    node_rows = [
        {"id": "a", "display_name": "A", "entity_type": "Concept",
         "mention_count": 3, "is_seed": True},
        {"id": "b", "display_name": "B", "entity_type": "Concept",
         "mention_count": 5, "is_seed": False},
    ]
    edge_rows = [
        {"source": "a", "target": "b", "predicate": "related_to",
         "confidence": 0.7}
    ]
    driver = _build_expand_driver(node_rows, edge_rows)
    out = await graph_query.expand_subgraph(
        entity_ids=["a"], corpus_id="corp-1", driver=driver, metrics=None
    )
    assert out["nodes"] == node_rows
    # No annotation fields leaked into the cold-cache path.
    for n in out["nodes"]:
        assert "pagerank_score" not in n
        assert "concept_id" not in n
        assert "is_working_entity" not in n


@pytest.mark.asyncio
async def test_expand_subgraph_warm_cache_annotates():
    """metrics warm → nodes annotated with pagerank / concept_id /
    is_working_entity. Original fields preserved."""
    node_rows = [
        {"id": "a", "display_name": "A", "entity_type": "Concept",
         "mention_count": 3, "is_seed": True},
        {"id": "b", "display_name": "B", "entity_type": "Concept",
         "mention_count": 5, "is_seed": False},
    ]
    edge_rows = [
        {"source": "a", "target": "b", "predicate": "related_to",
         "confidence": 0.7}
    ]
    driver = _build_expand_driver(node_rows, edge_rows)
    metrics = SimpleNamespace(
        top_pagerank=[
            {"entity_id": "a", "score": 0.42},
            {"entity_id": "b", "score": 0.18},
        ],
        entity_concept_map={
            "a": {"concept_id": 7},
            "b": {"concept_id": 7},  # same concept — diversification kicks in
        },
        # other fields needed by select_working_entities
        frontier_candidates=[],
        fragile_bridges=[],
        terminological_gaps=[],
        structural_analogies=[],
        transfer_candidates=[],
    )
    out = await graph_query.expand_subgraph(
        entity_ids=["a"], corpus_id="corp-1", driver=driver,
        metrics=metrics,
        entity_scores={"a": 0.9, "b": 0.6},
    )
    by_id = {n["id"]: n for n in out["nodes"]}
    # Both nodes have pagerank.
    assert by_id["a"]["pagerank_score"] == pytest.approx(0.42)
    assert by_id["b"]["pagerank_score"] == pytest.approx(0.18)
    # Both nodes carry concept_id (same concept 7 in this fixture).
    assert by_id["a"]["concept_id"] == "7"
    assert by_id["b"]["concept_id"] == "7"
    # is_working_entity is set on at least one (select_working_entities
    # picks a diversified working set from the BFS scope).
    assert any(n.get("is_working_entity") for n in out["nodes"])
    # Original fields preserved.
    assert by_id["a"]["is_seed"] is True
    assert by_id["a"]["mention_count"] == 3


@pytest.mark.asyncio
async def test_expand_subgraph_no_pagerank_overlap_still_annotates_working():
    """When metrics has no PR entries for these specific nodes, the
    pagerank_score field is omitted but other annotations (working
    entity, concept_id) can still apply."""
    node_rows = [
        {"id": "a", "display_name": "A", "entity_type": "Concept",
         "mention_count": 3, "is_seed": True},
    ]
    driver = _build_expand_driver(node_rows, [])
    metrics = SimpleNamespace(
        top_pagerank=[{"entity_id": "z-elsewhere", "score": 0.99}],
        entity_concept_map={"a": {"concept_id": 3}},
        frontier_candidates=[],
        fragile_bridges=[],
        terminological_gaps=[],
        structural_analogies=[],
        transfer_candidates=[],
    )
    out = await graph_query.expand_subgraph(
        entity_ids=["a"], corpus_id="corp-1", driver=driver,
        metrics=metrics,
    )
    n = out["nodes"][0]
    assert "pagerank_score" not in n  # no PR entry for "a"
    assert n.get("concept_id") == "3"


@pytest.mark.asyncio
async def test_expand_subgraph_select_working_entities_failure_graceful():
    """If select_working_entities raises (half-deserialized cache),
    the BFS rows still return — no exception escapes."""
    node_rows = [
        {"id": "a", "display_name": "A", "entity_type": "Concept",
         "mention_count": 1, "is_seed": True},
    ]
    driver = _build_expand_driver(node_rows, [])
    # Metrics object is intentionally MALFORMED — top_pagerank exists
    # but select_working_entities would raise on entity_concept_map
    # being None (not a dict). The expand_subgraph wrapper must catch
    # and continue.
    bad_metrics = SimpleNamespace(
        top_pagerank=[{"entity_id": "a", "score": 0.5}],
        entity_concept_map=None,  # will raise inside .get(...)
        frontier_candidates=[],
        fragile_bridges=[],
        terminological_gaps=[],
        structural_analogies=[],
        transfer_candidates=[],
    )
    out = await graph_query.expand_subgraph(
        entity_ids=["a"], corpus_id="corp-1", driver=driver,
        metrics=bad_metrics,
    )
    assert len(out["nodes"]) == 1
    # PageRank annotation might still land (it doesn't go through
    # select_working_entities), but the function must not have crashed.


@pytest.mark.asyncio
async def test_expand_subgraph_empty_seeds_short_circuits():
    """Empty entity_ids → empty result, no Cypher call (metrics
    irrelevant for this edge case)."""
    def on_run(*a, **k):  # pragma: no cover
        raise AssertionError("should not run")

    driver = _FakeDriver(on_run)
    out = await graph_query.expand_subgraph(
        entity_ids=[], corpus_id="corp-1", driver=driver,
        metrics=SimpleNamespace(top_pagerank=[]),
    )
    assert out == {"nodes": [], "links": []}


# ── find_gaps tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_gaps_cold_cache_missing_edge_only():
    """metrics=None → pre-Phase-3 missing-edge behavior, gap_type
    field stamped."""
    def on_run(cypher, **kwargs):
        return _FakeResult([
            {"entity_a_id": "a", "entity_a_name": "A",
             "entity_b_id": "b", "entity_b_name": "B"},
        ])

    driver = _FakeDriver(on_run)
    out = await graph_query.find_gaps(
        driver=driver, entity_ids=["a", "b"], metrics=None
    )
    assert len(out) == 1
    assert out[0]["gap_type"] == "missing_edge"


@pytest.mark.asyncio
async def test_find_gaps_terminological_when_warm():
    """metrics with a terminological_gaps entry touching a seed →
    emitted alongside missing-edge."""
    def on_run(cypher, **kwargs):
        return _FakeResult([])  # no missing edges in this fixture

    driver = _FakeDriver(on_run)
    metrics = SimpleNamespace(
        terminological_gaps=[{
            "source": "a", "source_name": "Habit Loop",
            "source_domain": "psychology",
            "target": "x9", "target_name": "Cue-Craving-Response-Reward",
            "target_domain": "neuroscience",
            "topology_sim": 0.82,
            "neighbor_jaccard": 0.55,
        }],
        structural_analogies=[],
        transfer_candidates=[],
    )
    out = await graph_query.find_gaps(
        driver=driver, entity_ids=["a", "b"], metrics=metrics
    )
    types = [g["gap_type"] for g in out]
    assert "terminological" in types
    term = next(g for g in out if g["gap_type"] == "terminological")
    assert term["entity_a_id"] == "a"
    assert term["entity_b_id"] == "x9"
    assert term["topology_sim"] == pytest.approx(0.82)
    assert "same concept" in term["question"].lower()


@pytest.mark.asyncio
async def test_find_gaps_analogy_when_warm():
    """metrics with a structural_analogies entry touching a seed →
    emitted with gap_type='analogy'."""
    def on_run(cypher, **kwargs):
        return _FakeResult([])

    driver = _FakeDriver(on_run)
    metrics = SimpleNamespace(
        terminological_gaps=[],
        structural_analogies=[{
            "source": "a", "source_name": "Network",
            "source_domain": "neuroscience",
            "target": "y7", "target_name": "Ecosystem",
            "target_domain": "biology",
            "topology_sim": 0.71,
            "neighbor_jaccard": 0.18,
        }],
        transfer_candidates=[],
    )
    out = await graph_query.find_gaps(
        driver=driver, entity_ids=["a"], metrics=metrics
    )
    analogies = [g for g in out if g["gap_type"] == "analogy"]
    assert len(analogies) == 1
    assert "relates" in analogies[0]["question"]


@pytest.mark.asyncio
async def test_find_gaps_transfer_flattens_target_domains():
    """transfer_candidates entries flatten — one row per
    (hub, target_domain) pair. The hub must be a seed."""
    def on_run(cypher, **kwargs):
        return _FakeResult([])

    driver = _FakeDriver(on_run)
    metrics = SimpleNamespace(
        terminological_gaps=[],
        structural_analogies=[],
        transfer_candidates=[{
            "hub": "a",   # this is a seed
            "hub_name": "Backpropagation",
            "hub_domain": "ml",
            "cd_pagerank": 0.42,
            "target_domains": ["physics", "biology"],
            "analogs": [
                {"entity": "p1", "name": "Gradient Descent",
                 "domain": "physics", "topology_sim": 0.7},
                {"entity": "b1", "name": "Neural Plasticity",
                 "domain": "biology", "topology_sim": 0.6},
            ],
        }],
    )
    out = await graph_query.find_gaps(
        driver=driver, entity_ids=["a"], metrics=metrics
    )
    transfers = [g for g in out if g["gap_type"] == "transfer"]
    assert len(transfers) == 2
    target_domains = {g["target_domain"] for g in transfers}
    assert target_domains == {"physics", "biology"}
    # Question text is grounded in the analytics — it references the
    # actual hub + target.
    for t in transfers:
        assert "approach" in t["question"].lower()


@pytest.mark.asyncio
async def test_find_gaps_warm_skips_entries_not_touching_seeds():
    """Warm-cache entries that don't touch the seed set are silently
    dropped — only seed-anchored gaps are surfaced."""
    def on_run(cypher, **kwargs):
        return _FakeResult([])

    driver = _FakeDriver(on_run)
    metrics = SimpleNamespace(
        terminological_gaps=[{
            "source": "off-seed-1", "target": "off-seed-2",
            "source_name": "Z1", "target_name": "Z2",
            "source_domain": "a", "target_domain": "b",
            "topology_sim": 0.9, "neighbor_jaccard": 0.7,
        }],
        structural_analogies=[],
        transfer_candidates=[{
            "hub": "off-seed-hub", "hub_name": "Z",
            "hub_domain": "ml",
            "target_domains": ["physics"],
            "analogs": [{"entity": "x", "name": "X",
                          "domain": "physics", "topology_sim": 0.5}],
        }],
    )
    out = await graph_query.find_gaps(
        driver=driver, entity_ids=["seed-a"], metrics=metrics
    )
    # No analytics gaps surfaced (none touched seed-a).
    assert all(g["gap_type"] == "missing_edge" for g in out)


@pytest.mark.asyncio
async def test_find_gaps_all_three_warm_types_plus_missing_edge():
    """End-to-end: all three analytics types AND the missing-edge
    Cypher results all show up in the same response, distinguished
    by gap_type."""
    def on_run(cypher, **kwargs):
        return _FakeResult([
            {"entity_a_id": "a", "entity_a_name": "A",
             "entity_b_id": "b", "entity_b_name": "B"},
        ])

    driver = _FakeDriver(on_run)
    metrics = SimpleNamespace(
        terminological_gaps=[{
            "source": "a", "target": "syn-1",
            "source_name": "A", "target_name": "Synonym1",
            "source_domain": "x", "target_domain": "y",
            "topology_sim": 0.8, "neighbor_jaccard": 0.6,
        }],
        structural_analogies=[{
            "source": "a", "target": "ana-1",
            "source_name": "A", "target_name": "Analog1",
            "source_domain": "x", "target_domain": "z",
            "topology_sim": 0.7, "neighbor_jaccard": 0.1,
        }],
        transfer_candidates=[{
            "hub": "b", "hub_name": "B", "hub_domain": "x",
            "target_domains": ["y"],
            "analogs": [{"entity": "tr-1", "name": "TX",
                          "domain": "y", "topology_sim": 0.5}],
        }],
    )
    out = await graph_query.find_gaps(
        driver=driver, entity_ids=["a", "b"], metrics=metrics
    )
    types = [g["gap_type"] for g in out]
    assert "terminological" in types
    assert "analogy" in types
    assert "transfer" in types
    assert "missing_edge" in types


@pytest.mark.asyncio
async def test_find_gaps_single_seed_warm_can_produce_results():
    """Phase 3 unlock — single-seed queries can produce gaps via the
    analytics path (the missing-edge fallback requires ≥2 seeds, but
    terminological / analogy / transfer don't)."""
    def on_run(*a, **k):  # pragma: no cover
        raise AssertionError("Cypher should not run for <2-seed query")

    driver = _FakeDriver(on_run)
    metrics = SimpleNamespace(
        terminological_gaps=[{
            "source": "a", "target": "syn-1",
            "source_name": "A", "target_name": "Synonym1",
            "source_domain": "x", "target_domain": "y",
            "topology_sim": 0.8, "neighbor_jaccard": 0.6,
        }],
        structural_analogies=[],
        transfer_candidates=[],
    )
    out = await graph_query.find_gaps(
        driver=driver, entity_ids=["a"], metrics=metrics
    )
    # Single seed, no missing-edge Cypher run, but terminological gap fires.
    assert len(out) == 1
    assert out[0]["gap_type"] == "terminological"


@pytest.mark.asyncio
async def test_find_gaps_empty_seeds_returns_empty():
    """Pre/post-Phase-3 invariant: empty seed list → empty result."""
    def on_run(*a, **k):  # pragma: no cover
        raise AssertionError("should not run")

    driver = _FakeDriver(on_run)
    out = await graph_query.find_gaps(
        driver=driver, entity_ids=[], metrics=SimpleNamespace()
    )
    assert out == []

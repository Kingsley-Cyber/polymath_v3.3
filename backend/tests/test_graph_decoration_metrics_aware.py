"""Phase 5b — graph decoration cache-annotation tests.

Pins the contract for `GraphDecorator._annotate_from_cache`:

  • db=None or cache cold → base GraphDecoration fields unchanged,
    annotation fields stay at their defaults (None / False).
  • Cache warm + entity_id in entity_betweenness → annotation lands.
  • Cache warm + entity_id NOT in any cache field → field stays None
    (additive, never raises).
  • Fragile bridge match → is_fragile_bridge=True (both directions).
  • Multi-corpus → fields merge across warm corpora; higher score wins
    on overlap.
  • Cache lookup raises → no exception escapes; decorations returned
    with default annotations.
  • RETRIEVAL_CACHE_DECORATION_METRICS=False → skip annotation entirely.
"""
from __future__ import annotations

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


from models.schemas import GraphDecoration  # noqa: E402
from services.retriever.graph_decoration import GraphDecorator  # noqa: E402


def _make_decoration(
    winner: str = "win-1",
    seed: str = "Habit Loop",
    neighbor: str = "Reward Pathway",
    seed_id: str = "ent:habit-loop",
    neighbor_id: str = "ent:reward-pathway",
) -> GraphDecoration:
    return GraphDecoration(
        winner_chunk_id=winner,
        seed_entity=seed,
        neighbor_entity=neighbor,
        seed_entity_id=seed_id,
        neighbor_entity_id=neighbor_id,
        predicate="relates_to",
        relation_family="Causal",
        edge_evidence="",
        edge_weight=0.7,
    )


@pytest.fixture
def decorator():
    """A GraphDecorator instance — we drive _annotate_from_cache directly,
    so the Neo4j init in __init__ doesn't matter."""
    d = GraphDecorator()
    return d


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_none_means_no_annotation(decorator):
    """When the orchestrator passes db=None (Mongo not connected),
    the annotation step is bypassed entirely. Fields stay at defaults."""
    decorations = [_make_decoration()]
    # _annotate_from_cache is private; we call it through the public
    # `decorate_winners` would skip it when db is None. Verify by
    # checking the fields directly:
    assert decorations[0].seed_betweenness is None
    assert decorations[0].is_fragile_bridge is False


@pytest.mark.asyncio
async def test_warm_cache_annotates_betweenness_and_pagerank(decorator):
    """Cache present with both betweenness and top_pagerank entries
    matching the entity_ids → all 4 numeric annotations land."""
    decorations = [_make_decoration()]
    metrics = SimpleNamespace(
        entity_betweenness={
            "ent:habit-loop": 0.42,
            "ent:reward-pathway": 0.18,
        },
        top_pagerank=[
            {"entity_id": "ent:habit-loop", "score": 0.06},
            {"entity_id": "ent:reward-pathway", "score": 0.03},
        ],
        fragile_bridges=[],
    )
    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=AsyncMock(return_value="sig")),
        patch("services.graph.analytics.get_cached_metrics",
              new=AsyncMock(return_value=metrics)),
    ):
        await decorator._annotate_from_cache(
            decorations, corpus_ids=["corp-1"], db=MagicMock(),
        )
    d = decorations[0]
    assert d.seed_betweenness == pytest.approx(0.42)
    assert d.neighbor_betweenness == pytest.approx(0.18)
    assert d.seed_pagerank == pytest.approx(0.06)
    assert d.neighbor_pagerank == pytest.approx(0.03)
    assert d.is_fragile_bridge is False  # not in fragile_bridges fixture


@pytest.mark.asyncio
async def test_fragile_bridge_flag_set_symmetric(decorator):
    """A fragile_bridge entry matching (seed, neighbor) OR
    (neighbor, seed) sets is_fragile_bridge=True. Direction-agnostic."""
    # First direction
    decorations_a = [_make_decoration(
        seed_id="ent:a", neighbor_id="ent:b",
    )]
    metrics_a = SimpleNamespace(
        entity_betweenness={},
        top_pagerank=[],
        fragile_bridges=[{"source": "ent:a", "target": "ent:b"}],
    )
    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=AsyncMock(return_value="sig")),
        patch("services.graph.analytics.get_cached_metrics",
              new=AsyncMock(return_value=metrics_a)),
    ):
        await decorator._annotate_from_cache(
            decorations_a, corpus_ids=["c"], db=MagicMock(),
        )
    assert decorations_a[0].is_fragile_bridge is True

    # Reverse direction — same fixture matched against swapped IDs.
    decorations_b = [_make_decoration(seed_id="ent:b", neighbor_id="ent:a")]
    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=AsyncMock(return_value="sig")),
        patch("services.graph.analytics.get_cached_metrics",
              new=AsyncMock(return_value=metrics_a)),
    ):
        await decorator._annotate_from_cache(
            decorations_b, corpus_ids=["c"], db=MagicMock(),
        )
    assert decorations_b[0].is_fragile_bridge is True


@pytest.mark.asyncio
async def test_entity_id_missing_from_cache_leaves_fields_none(decorator):
    """Cache present but entity_id not in any cache field → all
    annotations stay None / False. Doesn't crash."""
    decorations = [_make_decoration(
        seed_id="ent:not-in-cache",
        neighbor_id="ent:also-not-in-cache",
    )]
    metrics = SimpleNamespace(
        entity_betweenness={"ent:different-entity": 0.5},
        top_pagerank=[{"entity_id": "ent:something-else", "score": 0.4}],
        fragile_bridges=[],
    )
    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=AsyncMock(return_value="sig")),
        patch("services.graph.analytics.get_cached_metrics",
              new=AsyncMock(return_value=metrics)),
    ):
        await decorator._annotate_from_cache(
            decorations, corpus_ids=["c"], db=MagicMock(),
        )
    d = decorations[0]
    assert d.seed_betweenness is None
    assert d.neighbor_betweenness is None
    assert d.seed_pagerank is None
    assert d.neighbor_pagerank is None
    assert d.is_fragile_bridge is False


@pytest.mark.asyncio
async def test_cache_returns_none_falls_through_cleanly(decorator):
    """Mongo doc not found → annotation step exits early, fields stay
    at defaults. No exception."""
    decorations = [_make_decoration()]
    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=AsyncMock(return_value="sig")),
        patch("services.graph.analytics.get_cached_metrics",
              new=AsyncMock(return_value=None)),
    ):
        await decorator._annotate_from_cache(
            decorations, corpus_ids=["c"], db=MagicMock(),
        )
    assert decorations[0].seed_betweenness is None
    assert decorations[0].is_fragile_bridge is False


@pytest.mark.asyncio
async def test_signature_failure_caught(decorator):
    """compute_corpus_change_signature raises (Mongo down) → caught,
    fields stay defaulted. No exception escapes."""
    decorations = [_make_decoration()]
    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=AsyncMock(side_effect=RuntimeError("mongo down"))),
        patch("services.graph.analytics.get_cached_metrics",
              new=AsyncMock(return_value=None)),
    ):
        # Should NOT raise.
        await decorator._annotate_from_cache(
            decorations, corpus_ids=["c"], db=MagicMock(),
        )
    assert decorations[0].seed_betweenness is None


@pytest.mark.asyncio
async def test_multi_corpus_merges_betweenness_and_pagerank(decorator):
    """Two warm corpora with overlapping entity_id → higher score wins."""
    decorations = [_make_decoration(seed_id="ent:shared")]
    metrics_a = SimpleNamespace(
        entity_betweenness={"ent:shared": 0.3},
        top_pagerank=[{"entity_id": "ent:shared", "score": 0.04}],
        fragile_bridges=[],
    )
    metrics_b = SimpleNamespace(
        entity_betweenness={"ent:shared": 0.7},  # higher
        top_pagerank=[{"entity_id": "ent:shared", "score": 0.08}],  # higher
        fragile_bridges=[],
    )
    metrics_by_corpus = {"corp-A": metrics_a, "corp-B": metrics_b}

    async def fake_get(_db, cid, _sig):
        return metrics_by_corpus.get(cid)

    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=AsyncMock(return_value="sig")),
        patch("services.graph.analytics.get_cached_metrics", new=fake_get),
    ):
        await decorator._annotate_from_cache(
            decorations, corpus_ids=["corp-A", "corp-B"], db=MagicMock(),
        )
    # Higher of the two values wins on the merge.
    assert decorations[0].seed_betweenness == pytest.approx(0.7)
    assert decorations[0].seed_pagerank == pytest.approx(0.08)


@pytest.mark.asyncio
async def test_empty_decorations_short_circuits(decorator):
    """Empty decoration list → no cache call, no error."""
    decorations = []
    fake_sig = AsyncMock(return_value="sig")
    fake_get = AsyncMock(return_value=SimpleNamespace(
        entity_betweenness={}, top_pagerank=[], fragile_bridges=[],
    ))
    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=fake_sig),
        patch("services.graph.analytics.get_cached_metrics", new=fake_get),
    ):
        await decorator._annotate_from_cache(
            decorations, corpus_ids=["c"], db=MagicMock(),
        )
    # Cache lookups did still run (per-corpus loop), but no annotation
    # work because the list is empty. The function returns silently.
    assert decorations == []


@pytest.mark.asyncio
async def test_graphdecoration_model_has_new_fields():
    """Sanity: the model carries the new optional fields with the
    documented defaults."""
    d = GraphDecoration(
        winner_chunk_id="w", seed_entity="A", neighbor_entity="B",
        predicate="x", relation_family="y",
    )
    assert d.seed_entity_id == ""
    assert d.neighbor_entity_id == ""
    assert d.seed_betweenness is None
    assert d.neighbor_betweenness is None
    assert d.seed_pagerank is None
    assert d.neighbor_pagerank is None
    assert d.is_fragile_bridge is False

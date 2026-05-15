"""Phase 5b — Mode A bonus expansion (cache-driven bridge pass) tests.

Pins the contract for `ModeAExpansion._expand_via_bridges`:

  • db=None → returns [] (Mode A.expand falls back to mentions+calls only).
  • Cache cold for all corpora → returns [].
  • Cache warm + no bridge endpoint matches any seed entity → returns [].
  • Cache warm + fragile_bridges anchored to seed → bonus chunks emitted
    with `bridge_type="fragile"` provenance.
  • Cache warm + structural_analogies, terminological_gaps, transfer_candidates
    → each produces bonus chunks with the matching provenance label.
  • Bonus chunks are capped at `limit`. Pool size never exceeds the cap.
  • Synthetic scores stay in 0.0-1.0 range. Sort is desc by score.
  • A seed entity that overlaps with a bonus entity is NOT emitted as
    a bonus (no self-pulling).
  • Cypher failures (seed-entity lookup OR bonus fetch) → returns [].
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


from services.retriever.mode_a import ModeAExpansion  # noqa: E402


# ── Fake Neo4j driver — multi-script, the bonus path runs TWO queries ─


class _FakeRecord(dict):
    """Dict that also responds to .get() like a Neo4j Record does."""


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def __aiter__(self):
        return self._async_iter()

    async def _async_iter(self):
        for r in self._rows:
            yield r

    async def single(self):
        if self._rows:
            return self._rows[0]
        return None


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


def _make_expansion(driver):
    """Build a ModeAExpansion with the driver pre-installed (bypass
    the NEO4J_ENABLED gate in __init__)."""
    exp = ModeAExpansion()
    exp._driver = driver
    # Force the gate to pass.
    exp._settings.NEO4J_ENABLED = True
    return exp


def _driver_returning(seed_entities: list[str], bonus_rows: list[dict]):
    """First session.run() returns the seed entity list; second
    returns the bonus chunk rows. Mirrors _expand_via_bridges' two
    Cypher round-trips."""
    state = {"call": 0}

    def on_run(cypher, **kwargs):
        state["call"] += 1
        if state["call"] == 1:
            return _FakeResult([_FakeRecord(seed_entity_ids=seed_entities)])
        return _FakeResult([_FakeRecord(**r) for r in bonus_rows])

    return _FakeDriver(on_run)


# ── Tests ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_db_none_short_circuits_expand_via_bridges_in_full_expand():
    """When `expand()` is called with db=None, the bridge pass is
    skipped entirely. Mention + calls passes still run (they don't
    take db)."""
    exp = ModeAExpansion()
    exp._settings.NEO4J_ENABLED = True
    exp._driver = MagicMock()

    # Patch the mention and calls passes to return empty (they're not
    # what we're testing here), and verify _expand_via_bridges is NOT
    # called when db is None.
    with (
        patch.object(exp, "_expand_via_mentions",
                     new=AsyncMock(return_value=[])),
        patch.object(exp, "_expand_via_calls",
                     new=AsyncMock(return_value=[])),
        patch.object(exp, "_expand_via_bridges",
                     new=AsyncMock(return_value=[])) as bridge_mock,
    ):
        from models.schemas import SourceChunk
        merged_pool = [SourceChunk(
            chunk_id="seed-1", parent_id="", doc_id="d",
            corpus_id="c", text="", score=1.0, source_tier="qdrant_only",
        )]
        await exp.expand(merged_pool, corpus_ids=["c"], db=None)
    bridge_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_seed_entities_returns_empty():
    """If the seed-entity Cypher returns no entity_ids (chunks have
    no MENTIONS edges), bonus expansion bails."""
    driver = _driver_returning(seed_entities=[], bonus_rows=[])
    exp = _make_expansion(driver)
    out = await exp._expand_via_bridges(
        seed_ids=["seed-1"], corpus_ids=["c"], db=MagicMock(), limit=5,
    )
    assert out == []


@pytest.mark.asyncio
async def test_cold_cache_all_corpora_returns_empty():
    """Seed entities resolved, but no warm cache in any corpus →
    no bridges to match, no bonus chunks."""
    driver = _driver_returning(seed_entities=["ent:a"], bonus_rows=[])
    exp = _make_expansion(driver)
    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=AsyncMock(return_value="sig")),
        patch("services.graph.analytics.get_cached_metrics",
              new=AsyncMock(return_value=None)),
    ):
        out = await exp._expand_via_bridges(
            seed_ids=["seed-1"], corpus_ids=["c"], db=MagicMock(), limit=5,
        )
    assert out == []


@pytest.mark.asyncio
async def test_fragile_bridge_seed_match_emits_bonus_chunk():
    """Seed entity ent:a, fragile bridge ent:a→ent:b in cache,
    bonus Cypher returns a chunk mentioning ent:b → emitted with
    `bridge_type=fragile` provenance."""
    bonus_rows = [
        {"chunk_id": "bonus-1", "doc_id": "d1", "corpus_id": "c",
         "mention_conf": 0.85, "via_entity_id": "ent:b"},
    ]
    driver = _driver_returning(seed_entities=["ent:a"], bonus_rows=bonus_rows)
    exp = _make_expansion(driver)
    metrics = SimpleNamespace(
        fragile_bridges=[{
            "source": "ent:a", "target": "ent:b",
            "source_name": "SeedAlpha", "target_name": "BridgeBravo",
            "path_count": 1,
        }],
        structural_analogies=[],
        terminological_gaps=[],
        transfer_candidates=[],
    )
    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=AsyncMock(return_value="sig")),
        patch("services.graph.analytics.get_cached_metrics",
              new=AsyncMock(return_value=metrics)),
    ):
        out = await exp._expand_via_bridges(
            seed_ids=["seed-1"], corpus_ids=["c"], db=MagicMock(), limit=5,
        )
    assert len(out) == 1
    chunk = out[0]
    assert chunk.chunk_id == "bonus-1"
    # Score stayed in [0, 1] range.
    assert 0.0 < chunk.score <= 1.0
    # Provenance carries the bridge type for downstream rendering.
    assert chunk.provenance and len(chunk.provenance) == 1
    prov = chunk.provenance[0]
    assert prov["via"] == "bridge"
    assert prov["bridge_type"] == "fragile"
    assert prov["via_entity"] == "SeedAlpha"


@pytest.mark.asyncio
async def test_terminological_gap_emits_bonus_chunk():
    """Synonym-like pair → bridge_type='terminological'."""
    bonus_rows = [
        {"chunk_id": "bonus-2", "doc_id": "d", "corpus_id": "c",
         "mention_conf": 0.7, "via_entity_id": "ent:synonym"},
    ]
    driver = _driver_returning(seed_entities=["ent:term-a"], bonus_rows=bonus_rows)
    exp = _make_expansion(driver)
    metrics = SimpleNamespace(
        fragile_bridges=[],
        structural_analogies=[],
        terminological_gaps=[{
            "source": "ent:term-a", "target": "ent:synonym",
            "source_name": "Habit Loop", "target_name": "Cue-Reward Cycle",
            "topology_sim": 0.8, "neighbor_jaccard": 0.6,
        }],
        transfer_candidates=[],
    )
    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=AsyncMock(return_value="sig")),
        patch("services.graph.analytics.get_cached_metrics",
              new=AsyncMock(return_value=metrics)),
    ):
        out = await exp._expand_via_bridges(
            seed_ids=["seed-1"], corpus_ids=["c"], db=MagicMock(), limit=5,
        )
    assert len(out) == 1
    assert out[0].provenance[0]["bridge_type"] == "terminological"


@pytest.mark.asyncio
async def test_structural_analogy_emits_bonus_chunk():
    bonus_rows = [
        {"chunk_id": "bonus-3", "doc_id": "d", "corpus_id": "c",
         "mention_conf": 0.6, "via_entity_id": "ent:analog"},
    ]
    driver = _driver_returning(seed_entities=["ent:a"], bonus_rows=bonus_rows)
    exp = _make_expansion(driver)
    metrics = SimpleNamespace(
        fragile_bridges=[],
        structural_analogies=[{
            "source": "ent:a", "target": "ent:analog",
            "source_name": "Network", "target_name": "Ecosystem",
            "topology_sim": 0.7, "neighbor_jaccard": 0.1,
        }],
        terminological_gaps=[],
        transfer_candidates=[],
    )
    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=AsyncMock(return_value="sig")),
        patch("services.graph.analytics.get_cached_metrics",
              new=AsyncMock(return_value=metrics)),
    ):
        out = await exp._expand_via_bridges(
            seed_ids=["seed-1"], corpus_ids=["c"], db=MagicMock(), limit=5,
        )
    assert len(out) == 1
    assert out[0].provenance[0]["bridge_type"] == "analogy"


@pytest.mark.asyncio
async def test_transfer_candidate_flattens_analogs_to_bonus_chunks():
    """transfer_candidates with multiple analogs in different domains →
    one bonus chunk per analog (when the bonus Cypher returns rows
    for each analog)."""
    bonus_rows = [
        {"chunk_id": "physics-chunk", "doc_id": "dp", "corpus_id": "c",
         "mention_conf": 0.5, "via_entity_id": "ent:physics-method"},
        {"chunk_id": "bio-chunk", "doc_id": "db", "corpus_id": "c",
         "mention_conf": 0.55, "via_entity_id": "ent:bio-method"},
    ]
    driver = _driver_returning(seed_entities=["ent:hub"], bonus_rows=bonus_rows)
    exp = _make_expansion(driver)
    metrics = SimpleNamespace(
        fragile_bridges=[],
        structural_analogies=[],
        terminological_gaps=[],
        transfer_candidates=[{
            "hub": "ent:hub", "hub_name": "Backpropagation", "hub_domain": "ml",
            "target_domains": ["physics", "biology"],
            "analogs": [
                {"entity": "ent:physics-method", "name": "Gradient Descent",
                 "domain": "physics", "topology_sim": 0.65},
                {"entity": "ent:bio-method", "name": "Neural Plasticity",
                 "domain": "biology", "topology_sim": 0.55},
            ],
        }],
    )
    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=AsyncMock(return_value="sig")),
        patch("services.graph.analytics.get_cached_metrics",
              new=AsyncMock(return_value=metrics)),
    ):
        out = await exp._expand_via_bridges(
            seed_ids=["seed-1"], corpus_ids=["c"], db=MagicMock(), limit=5,
        )
    assert len(out) == 2
    assert all(c.provenance[0]["bridge_type"] == "transfer" for c in out)


@pytest.mark.asyncio
async def test_seed_entity_not_re_emitted_as_bonus():
    """If a bridge endpoint coincidentally matches a seed entity_id,
    that entity is NOT emitted as a bonus (no self-pulling)."""
    driver = _driver_returning(
        seed_entities=["ent:a", "ent:b"],  # both seeds
        bonus_rows=[],
    )
    exp = _make_expansion(driver)
    metrics = SimpleNamespace(
        fragile_bridges=[{
            "source": "ent:a", "target": "ent:b",
            "source_name": "A", "target_name": "B",
            "path_count": 1,
        }],
        structural_analogies=[],
        terminological_gaps=[],
        transfer_candidates=[],
    )
    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=AsyncMock(return_value="sig")),
        patch("services.graph.analytics.get_cached_metrics",
              new=AsyncMock(return_value=metrics)),
    ):
        out = await exp._expand_via_bridges(
            seed_ids=["seed-1"], corpus_ids=["c"], db=MagicMock(), limit=5,
        )
    # Both endpoints are seeds → _consider() skips both, bonus_scores
    # is empty → no Cypher round-trip 2 happens → empty list.
    assert out == []


@pytest.mark.asyncio
async def test_bonus_pool_respects_limit():
    """If the bonus Cypher returns more chunks than `limit`, only
    the top-N by score are returned."""
    bonus_rows = [
        {"chunk_id": f"bonus-{i}", "doc_id": "d", "corpus_id": "c",
         "mention_conf": 0.9 - i * 0.1,  # decreasing confidence
         "via_entity_id": "ent:b"}
        for i in range(8)
    ]
    driver = _driver_returning(seed_entities=["ent:a"], bonus_rows=bonus_rows)
    exp = _make_expansion(driver)
    metrics = SimpleNamespace(
        fragile_bridges=[{
            "source": "ent:a", "target": "ent:b",
            "source_name": "A", "target_name": "B",
            "path_count": 1,
        }],
        structural_analogies=[],
        terminological_gaps=[],
        transfer_candidates=[],
    )
    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=AsyncMock(return_value="sig")),
        patch("services.graph.analytics.get_cached_metrics",
              new=AsyncMock(return_value=metrics)),
    ):
        out = await exp._expand_via_bridges(
            seed_ids=["seed-1"], corpus_ids=["c"], db=MagicMock(), limit=3,
        )
    # Cap at limit=3.
    assert len(out) == 3
    # Sort is desc by score (mention_conf descending).
    scores = [c.score for c in out]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_synthetic_scores_stay_in_unit_range():
    """Bonus scores are bounded at 1.0 — even max bridge_score × max
    mention_conf can't exceed it."""
    bonus_rows = [
        {"chunk_id": "bonus-1", "doc_id": "d", "corpus_id": "c",
         "mention_conf": 1.0, "via_entity_id": "ent:b"},
    ]
    driver = _driver_returning(seed_entities=["ent:a"], bonus_rows=bonus_rows)
    exp = _make_expansion(driver)
    metrics = SimpleNamespace(
        fragile_bridges=[{
            "source": "ent:a", "target": "ent:b",
            "source_name": "A", "target_name": "B",
            "path_count": 10,  # high path_count, still bounded
        }],
        structural_analogies=[],
        terminological_gaps=[],
        transfer_candidates=[],
    )
    with (
        patch("services.graph.analytics.compute_corpus_change_signature",
              new=AsyncMock(return_value="sig")),
        patch("services.graph.analytics.get_cached_metrics",
              new=AsyncMock(return_value=metrics)),
    ):
        out = await exp._expand_via_bridges(
            seed_ids=["seed-1"], corpus_ids=["c"], db=MagicMock(), limit=5,
        )
    assert len(out) == 1
    assert 0.0 < out[0].score <= 1.0


@pytest.mark.asyncio
async def test_seed_cypher_failure_returns_empty():
    """If the seed-entity-lookup Cypher raises, the function returns
    [] (Mode A.expand will continue with mentions+calls)."""
    def on_run(*a, **k):
        raise RuntimeError("neo4j unreachable")

    driver = _FakeDriver(on_run)
    exp = _make_expansion(driver)
    out = await exp._expand_via_bridges(
        seed_ids=["seed-1"], corpus_ids=["c"], db=MagicMock(), limit=5,
    )
    assert out == []

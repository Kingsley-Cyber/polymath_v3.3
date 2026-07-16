"""Sprint #1 — graph-degree boost unit tests.

These tests pin the multiplier formula and the pool-ordering behavior
to a regression-safe shape. The full `apply_graph_degree_boost` flow
is an integration test (needs Neo4j), so unit-level we exercise the
pure `compute_multiplier` helper and the in-memory chunk-mutation
path with a stubbed Cypher result.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.retriever.graph_rerank import (
    DEFAULT_ALPHA,
    MAX_DEGREE_CAP,
    apply_graph_degree_boost,
    compute_multiplier,
)


# ─── Multiplier formula pins ───────────────────────────────────────────────


def test_compute_multiplier_zero_degree_no_boost():
    assert compute_multiplier(0) == 1.0
    assert compute_multiplier(-3) == 1.0  # defensive


def test_compute_multiplier_monotonic_in_degree():
    a = compute_multiplier(1)
    b = compute_multiplier(10)
    c = compute_multiplier(50)
    assert 1.0 < a < b < c


def test_compute_multiplier_caps_at_max_degree():
    """Degrees above MAX_DEGREE_CAP should produce identical multipliers
    — the cap is the whole point of the cap."""
    capped = compute_multiplier(MAX_DEGREE_CAP)
    over = compute_multiplier(MAX_DEGREE_CAP + 1000)
    assert capped == over


def test_compute_multiplier_exact_formula():
    """Pin the formula so refactors don't silently change behavior."""
    expected_at_10 = 1.0 + DEFAULT_ALPHA * math.log1p(10)
    assert compute_multiplier(10) == pytest.approx(expected_at_10)


def test_compute_multiplier_max_boost_bounded():
    """Even the absolute max boost shouldn't more-than-double a score —
    the cross-encoder needs room to overrule."""
    max_mult = compute_multiplier(10_000)  # gets capped to MAX_DEGREE_CAP
    assert max_mult < 2.0


# ─── In-memory boost path ──────────────────────────────────────────────────


def _chunk(chunk_id: str, score: float) -> SimpleNamespace:
    return SimpleNamespace(chunk_id=chunk_id, corpus_id="c1", score=score)


def _stub_neo4j_driver(degrees_by_chunk_id: dict[str, int]):
    """Mock that returns the supplied degrees from a single session.run."""

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def run(self, _cypher: str, **kwargs):
            chunk_refs = kwargs.get("chunk_refs", [])

            async def _gen():
                for ref in chunk_refs:
                    cid = ref["chunk_id"]
                    if cid in degrees_by_chunk_id:
                        yield {
                            "corpus_id": ref["corpus_id"],
                            "chunk_id": cid,
                            "max_degree": degrees_by_chunk_id[cid],
                        }

            class _Result:
                def __aiter__(self_inner):
                    return _gen()

            return _Result()

    driver = MagicMock()
    driver.session = MagicMock(return_value=_Session())
    return driver


@pytest.mark.asyncio
async def test_apply_boost_hub_chunk_outranks_obscure_chunk():
    """The key property: a chunk that mentions a high-degree entity
    should rank above a chunk with a slightly higher base score but
    only obscure mentions."""
    hub_chunk = _chunk("hub", score=1.00)
    obscure_chunk = _chunk("obscure", score=1.05)
    chunks = [hub_chunk, obscure_chunk]

    driver = _stub_neo4j_driver(
        {"hub": 50, "obscure": 1},
    )
    await apply_graph_degree_boost(chunks, ["c1"], driver)
    # After boost: hub × ~1.59, obscure × ~1.10
    assert hub_chunk.score > obscure_chunk.score


@pytest.mark.asyncio
async def test_apply_boost_no_op_without_neo4j_driver():
    chunks = [_chunk("a", 1.0)]
    await apply_graph_degree_boost(chunks, ["c1"], neo4j_driver=None)
    assert chunks[0].score == 1.0


@pytest.mark.asyncio
async def test_apply_boost_no_op_on_empty_pool():
    out = await apply_graph_degree_boost([], ["c1"], _stub_neo4j_driver({}))
    assert out == []


@pytest.mark.asyncio
async def test_apply_boost_no_op_when_no_mentions():
    """If no chunk has :MENTIONS edges (pure-prose corpus with
    use_neo4j=False), the boost path returns the pool untouched."""
    chunks = [_chunk("a", 1.0), _chunk("b", 0.5)]
    driver = _stub_neo4j_driver({})  # empty result
    await apply_graph_degree_boost(chunks, ["c1"], driver)
    assert chunks[0].score == 1.0
    assert chunks[1].score == 0.5


@pytest.mark.asyncio
async def test_apply_boost_preserves_relative_order_within_same_degree():
    """Two chunks with the same max_degree should keep their original
    relative score ordering (the boost is a constant multiplier)."""
    a = _chunk("a", 1.0)
    b = _chunk("b", 2.0)
    driver = _stub_neo4j_driver({"a": 10, "b": 10})
    await apply_graph_degree_boost([a, b], ["c1"], driver)
    assert b.score > a.score


@pytest.mark.asyncio
async def test_apply_boost_handles_cypher_error():
    """Neo4j hiccup must not blow up retrieval — return the pool
    untouched and let the cross-encoder rerank as if no boost ran."""
    chunks = [_chunk("a", 1.0)]

    class _BrokenSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def run(self, *_a, **_kw):
            raise RuntimeError("cypher boom")

    driver = MagicMock()
    driver.session = MagicMock(return_value=_BrokenSession())

    await apply_graph_degree_boost(chunks, ["c1"], driver)
    assert chunks[0].score == 1.0

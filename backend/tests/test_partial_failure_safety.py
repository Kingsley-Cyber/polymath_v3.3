"""Partial-failure safety tests — Bug #1 + #2 from the audit.

Pins the contract for the new `_unwrap_funnel_result` helper +
the graph_query router's `_gated` wrapper:

  • Single funnel raising an exception → that funnel contributes
    [] but the others' results still flow through.
  • Multiple funnels raising → orchestrator still returns a valid
    RetrievalResult-shaped response, just with thinner pools.
  • Graph query: one corpus's _run_one raising → that corpus
    contributes an empty result struct, other corpora unaffected.

The functions tested here previously bubbled exceptions up through
asyncio.gather → 500'd the entire chat / graph query turn.
"""
from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

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


from services.retriever import _unwrap_funnel_result  # noqa: E402
from models.schemas import SourceChunk  # noqa: E402


def _chunk(cid: str, score: float = 0.5) -> SourceChunk:
    return SourceChunk(
        chunk_id=cid, parent_id="", doc_id="d", corpus_id="c",
        text="", score=score, source_tier="qdrant_only",
    )


# ── _unwrap_funnel_result tests ─────────────────────────────────────


def test_unwrap_returns_list_unchanged_on_normal_result():
    """Happy path: a real list of chunks passes through as-is."""
    chunks = [_chunk("c1"), _chunk("c2")]
    out = _unwrap_funnel_result(chunks, "funnel_a")
    assert out is not chunks  # list() copy
    assert len(out) == 2
    assert [c.chunk_id for c in out] == ["c1", "c2"]


def test_unwrap_converts_exception_to_empty_list():
    """The critical fix: exception → empty list, NOT bubbled up."""
    err = ConnectionError("qdrant timeout")
    out = _unwrap_funnel_result(err, "funnel_a")
    assert out == []


def test_unwrap_converts_runtime_error_to_empty_list():
    """Any subclass of BaseException → empty list."""
    err = RuntimeError("mongo disconnected mid-query")
    out = _unwrap_funnel_result(err, "funnel_b")
    assert out == []


def test_unwrap_handles_none_result():
    """Defensive: None (shouldn't happen in practice, but cheap to handle)."""
    out = _unwrap_funnel_result(None, "lexical")
    assert out == []


def test_unwrap_empty_list_passes_through():
    """Empty result is valid — passes through unchanged."""
    out = _unwrap_funnel_result([], "funnel_a")
    assert out == []


# ── Integration: simulate asyncio.gather with mixed success/failure ─


@pytest.mark.asyncio
async def test_mixed_funnel_results_partial_failure_flows_through():
    """End-to-end shape: gather returns [chunks, exc, chunks] →
    _unwrap turns each into a usable list. The retrieve() caller can
    proceed with merge_pools on partial data."""
    import asyncio

    async def good() -> list[SourceChunk]:
        return [_chunk("good-1"), _chunk("good-2")]

    async def bad() -> list[SourceChunk]:
        raise ConnectionError("qdrant cluster unreachable")

    async def lexical() -> list[SourceChunk]:
        return [_chunk("lex-1")]

    raw_a, raw_b, raw_lex = await asyncio.gather(
        good(), bad(), lexical(), return_exceptions=True,
    )
    a = _unwrap_funnel_result(raw_a, "funnel_a")
    b = _unwrap_funnel_result(raw_b, "funnel_b")
    lex = _unwrap_funnel_result(raw_lex, "lexical")
    # Pre-fix: gather would have raised ConnectionError. Post-fix:
    # b is [], a + lexical preserved.
    assert len(a) == 2
    assert b == []  # funnel_b's exception → empty
    assert len(lex) == 1


@pytest.mark.asyncio
async def test_all_funnels_failing_still_returns_lists_not_exception():
    """Worst case — every funnel raises. Caller gets three empty
    lists, the chat turn produces a no-results response, NOT a 500."""
    import asyncio

    async def fail() -> list[SourceChunk]:
        raise RuntimeError("network down")

    raw = await asyncio.gather(fail(), fail(), fail(), return_exceptions=True)
    unwrapped = [_unwrap_funnel_result(r, f"f{i}") for i, r in enumerate(raw)]
    assert all(u == [] for u in unwrapped)
    # The merge step downstream handles 3 empty pools as "no results."
    # Caller never sees the exceptions.


# ── Graph query router _gated wrapper test ──────────────────────────


@pytest.mark.asyncio
async def test_graph_query_run_one_failure_returns_sentinel():
    """Bug #2 fix: simulate the `_gated` wrapper's behavior — when
    `_run_one` raises on one corpus, the wrapper returns a sentinel
    (cid, empty-result-dict) tuple. asyncio.gather keeps moving.

    We don't test the actual router (heavy import path); we verify
    the contract: a failing inner function gets converted to a
    structured empty result, NOT propagated."""
    import asyncio
    import logging

    sem = asyncio.Semaphore(4)

    async def _run_one_fake(cid: str):
        if cid == "corp-broken":
            raise RuntimeError("neo4j read timeout")
        return cid, {
            "nodes": [{"id": f"node-{cid}"}],
            "links": [],
            "bridges": [],
            "gaps": [],
            "seeds": [],
        }

    # Mirror the wrapper from routers/graph.py
    async def _gated(cid: str):
        async with sem:
            try:
                return await _run_one_fake(cid)
            except Exception as exc:
                logging.getLogger().warning(
                    "graph_query: corpus=%s failed: %s", cid, exc
                )
                return cid, {
                    "nodes": [], "links": [], "bridges": [],
                    "gaps": [], "seeds": [],
                }

    results = await asyncio.gather(*[
        _gated(c) for c in ["corp-A", "corp-broken", "corp-C"]
    ])

    # All 3 corpora returned a structured tuple. The broken one
    # returned an empty-result dict, NOT an exception.
    assert len(results) == 3
    cid_to_result = {cid: result for cid, result in results}
    assert "corp-A" in cid_to_result
    assert "corp-broken" in cid_to_result
    assert "corp-C" in cid_to_result
    # Broken corpus contributes empty result.
    assert cid_to_result["corp-broken"]["nodes"] == []
    assert cid_to_result["corp-broken"]["bridges"] == []
    # Healthy corpora still have their data.
    assert len(cid_to_result["corp-A"]["nodes"]) == 1
    assert len(cid_to_result["corp-C"]["nodes"]) == 1

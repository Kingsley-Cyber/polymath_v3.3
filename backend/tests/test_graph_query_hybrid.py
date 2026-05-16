"""Phase 1 hybrid seed-extraction tests.

Pins the contract for `extract_query_entities`'s new hybrid behavior:

  • Path A (literal CONTAINS) still works exactly as before when no
    qdrant client is provided.
  • Path B (vector scope via analytics.query_scope_entities) augments
    the literal seeds when qdrant is wired — synonym-only hits surface
    in the result with vector_match=True.
  • Both paths converging on the same entity strengthens the score
    (sources=["literal","vector"]).
  • Qdrant failure falls back to the literal path cleanly — never
    crashes the extractor.

Mocks the Neo4j driver + qdrant client so the test doesn't require a
running cluster. The actual Cypher is run through a fake session.run
that returns canned rows shaped like the real query.
"""
from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Auth-package stubs (same pattern as the MCP / models tests) ──────
def _install_stubs_if_missing() -> None:
    for mod_name in ("jose", "passlib", "passlib.context", "slowapi", "slowapi.util"):
        try:
            __import__(mod_name)
        except ImportError:
            pass
        else:
            continue

    # jose
    if "jose" not in sys.modules:
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

    # passlib
    if "passlib.context" not in sys.modules:
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

    # slowapi
    if "slowapi" not in sys.modules:
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


# ── Fake Neo4j driver / session / result that returns canned rows ────


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


def _row(entity_id: str, display_name: str, mention_count: int):
    """Shape that matches the Cypher RETURN clause."""
    return {
        "entity_id": entity_id,
        "display_name": display_name,
        "entity_type": "Concept",
        "mention_count": mention_count,
    }


# ── Tests ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_literal_only_path_when_no_qdrant():
    """Phase-1 backwards-compat: no qdrant → behaves exactly like
    the pre-Phase-1 CONTAINS-only path."""
    captured_kwargs = {}

    def on_run(cypher, **kwargs):
        captured_kwargs.update(kwargs)
        return _FakeResult([_row("e1", "Habit Formation", 12)])

    driver = _FakeDriver(on_run)
    result = await graph_query.extract_query_entities(
        "habit formation in behavioral science",
        "corp-1",
        driver,
        qdrant=None,
    )

    # The Cypher received empty vector_seed_ids — literal path only.
    assert captured_kwargs["vector_seed_ids"] == []
    assert len(result) == 1
    r = result[0]
    assert r["entity_id"] == "e1"
    # Sources reflect literal hit (token "habit" or "formation" in display_name).
    assert "literal" in r["sources"]
    assert "vector" not in r["sources"]
    assert r["vector_match"] is False
    assert r["score"] > 0


@pytest.mark.asyncio
async def test_vector_path_surfaces_synonym_miss():
    """The key Phase-1 win: a query that uses a different word than
    the entity's display_name still surfaces the entity via the
    vector seed set."""
    captured_kwargs = {}

    def on_run(cypher, **kwargs):
        captured_kwargs.update(kwargs)
        # The entity's display_name has zero token overlap with
        # "habit formation" — pure synonym/paraphrase case.
        return _FakeResult([_row("e2", "Behavioral Routine Loop", 7)])

    driver = _FakeDriver(on_run)
    # Mock the vector path to return e2's id — simulating Qdrant
    # finding semantically similar chunks that mention e2.
    fake_qdrant = MagicMock()
    with patch.object(
        graph_query,
        "extract_query_entities",
        wraps=graph_query.extract_query_entities,
    ):
        with patch(
            "services.graph.analytics.query_scope_entities",
            new=AsyncMock(return_value={"e2"}),
        ):
            result = await graph_query.extract_query_entities(
                "habit formation in behavioral science",
                "corp-1",
                driver,
                qdrant=fake_qdrant,
            )

    assert captured_kwargs["vector_seed_ids"] == ["e2"]
    assert len(result) == 1
    r = result[0]
    assert r["entity_id"] == "e2"
    assert r["vector_match"] is True
    assert "vector" in r["sources"]
    # Literal overlap is 0 ("habit", "formation", "behavioral", "science" vs
    # "Behavioral Routine Loop" — "behavioral" matches). Hmm — wait, that DOES
    # overlap. Let me make the test stricter with a clearer synonym case.
    assert r["score"] > 0


@pytest.mark.asyncio
async def test_vector_path_pure_synonym_zero_overlap():
    """Tightens the synonym case — display_name shares NO tokens with
    the query. Vector path is the only way this entity surfaces."""
    def on_run(cypher, **kwargs):
        return _FakeResult([_row("e3", "Cue Craving Response Reward", 5)])

    driver = _FakeDriver(on_run)
    fake_qdrant = MagicMock()
    with patch(
        "services.graph.analytics.query_scope_entities",
        new=AsyncMock(return_value={"e3"}),
    ):
        result = await graph_query.extract_query_entities(
            "habit formation",
            "corp-1",
            driver,
            qdrant=fake_qdrant,
        )

    assert len(result) == 1
    r = result[0]
    assert r["vector_match"] is True
    # Zero token overlap with "Cue Craving Response Reward" → sources is
    # vector-only.
    assert r["sources"] == ["vector"]
    # The vector bonus floor (0.5 * mentions) ensures synonym-only hits
    # still score above zero.
    assert r["score"] > 0


@pytest.mark.asyncio
async def test_convergence_both_paths_strongest_signal():
    """When BOTH literal AND vector agree on an entity, sources lists
    both — that's the highest-confidence seed."""
    def on_run(cypher, **kwargs):
        return _FakeResult([_row("e4", "Habit Formation", 12)])

    driver = _FakeDriver(on_run)
    fake_qdrant = MagicMock()
    with patch(
        "services.graph.analytics.query_scope_entities",
        new=AsyncMock(return_value={"e4"}),
    ):
        result = await graph_query.extract_query_entities(
            "habit formation",
            "corp-1",
            driver,
            qdrant=fake_qdrant,
        )

    assert len(result) == 1
    r = result[0]
    assert "literal" in r["sources"]
    assert "vector" in r["sources"]
    assert r["vector_match"] is True


@pytest.mark.asyncio
async def test_qdrant_failure_falls_back_to_literal():
    """The vector path is best-effort. If Qdrant or the embedder
    raises, the literal path still produces results — never crashes."""
    def on_run(cypher, **kwargs):
        return _FakeResult([_row("e5", "Habit Formation", 12)])

    driver = _FakeDriver(on_run)
    fake_qdrant = MagicMock()
    with patch(
        "services.graph.analytics.query_scope_entities",
        new=AsyncMock(side_effect=RuntimeError("qdrant: connection refused")),
    ):
        result = await graph_query.extract_query_entities(
            "habit formation",
            "corp-1",
            driver,
            qdrant=fake_qdrant,
        )

    assert len(result) == 1
    r = result[0]
    assert "literal" in r["sources"]
    assert r["vector_match"] is False


@pytest.mark.asyncio
async def test_empty_query_returns_empty():
    """Edge case: empty query → empty result, no Cypher call."""
    def on_run(cypher, **kwargs):  # pragma: no cover — should not be called
        raise AssertionError("Cypher should not run on empty query")

    driver = _FakeDriver(on_run)
    result = await graph_query.extract_query_entities("", "corp-1", driver)
    assert result == []


@pytest.mark.asyncio
async def test_short_query_skips_vector_path():
    """Vector path requires query.strip() >= 3 chars — guard against
    embedding cost on noise like single-letter inputs."""
    captured = {}

    def on_run(cypher, **kwargs):
        captured.update(kwargs)
        return _FakeResult([])

    driver = _FakeDriver(on_run)
    fake_qdrant = MagicMock()
    scope_mock = AsyncMock(return_value=set())
    with patch(
        "services.graph.analytics.query_scope_entities",
        new=scope_mock,
    ):
        await graph_query.extract_query_entities(
            "ai",  # 2 chars
            "corp-1",
            driver,
            qdrant=fake_qdrant,
        )

    # query_scope_entities should NOT have been called for a 2-char query.
    scope_mock.assert_not_called()


@pytest.mark.asyncio
async def test_ai_acronym_does_not_match_inside_unrelated_words():
    """Two-letter acronyms must match as tokens, not substrings.

    Regression for "AI" matching domAIn / grAIned / contAIner in the Graph
    Query entity panel.
    """
    captured = {}

    def on_run(cypher, **kwargs):
        captured.update(kwargs)
        return _FakeResult([
            _row("domain", "Domain Model", 97),
            _row("lock", "Coarse-Grained Lock", 15),
            _row("container", "EJB container", 5),
            _row("ai", "AI", 3),
            _row("artificial", "Artificial Intelligence", 2),
        ])

    driver = _FakeDriver(on_run)
    result = await graph_query.extract_query_entities(
        "how does ai power chatrooms",
        "corp-1",
        driver,
        qdrant=None,
    )

    assert captured["exact_short_tokens"] == ["ai"]
    assert "ai" not in captured["contains_tokens"]
    ids = [r["entity_id"] for r in result]
    assert set(ids) == {"ai", "artificial"}
    assert "domain" not in ids
    assert "lock" not in ids
    assert "container" not in ids


@pytest.mark.asyncio
async def test_vector_only_no_tokens():
    """If the query produces no useful tokens (all stop-words) but
    the vector path returns seeds, those vector seeds still surface."""
    def on_run(cypher, **kwargs):
        return _FakeResult([_row("e6", "Some Concept", 4)])

    driver = _FakeDriver(on_run)
    fake_qdrant = MagicMock()
    with patch(
        "services.graph.analytics.query_scope_entities",
        new=AsyncMock(return_value={"e6"}),
    ):
        result = await graph_query.extract_query_entities(
            "what is the and of",  # all stop-words
            "corp-1",
            driver,
            qdrant=fake_qdrant,
        )

    # Despite zero useful tokens, the vector path produced a seed.
    assert len(result) == 1
    assert result[0]["sources"] == ["vector"]

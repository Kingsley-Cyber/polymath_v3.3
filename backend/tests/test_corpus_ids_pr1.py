"""PR 1 — multi-corpus input normalization + Pydantic dual-field tests.

Covers:
  * utils.corpus_ids.normalize_corpus_ids resolution rules
  * utils.corpus_ids.is_multi_corpus_disabled env-var reading
  * utils.corpus_ids.MultiCorpusDisabledError when kill switch active
  * utils.corpus_ids.compute_multi_corpus_signature determinism + order
  * GraphDiscoverRequest accepts legacy corpus_id and new corpus_ids
  * GraphQueryRequest accepts legacy corpus_id and new corpus_ids
  * GraphResumeCandidateRequest same
  * GraphDiscoverSession syncs corpus_id ↔ corpus_ids
  * GraphDiscoverResponse / GraphInsightPacket carry both fields
  * Subtypes (ContextGraphNode/Link, GraphInsightPacketEntity/Edge) carry
    source_corpus + source_corpora defaults

These are pure-Python tests with no DB/Neo4j/Qdrant dependencies. They
verify only the input-normalization layer of the multi-corpus rollout.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import pytest


# ─── utils.corpus_ids ─────────────────────────────────────────────────────────


@contextmanager
def _env(key: str, value: str | None):
    """Set/unset an env var for the scope of the with block, then restore."""
    original = os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    try:
        yield
    finally:
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original


def test_normalize_returns_empty_when_both_args_falsy():
    from utils.corpus_ids import normalize_corpus_ids

    assert normalize_corpus_ids() == []
    assert normalize_corpus_ids(corpus_id=None, corpus_ids=None) == []
    assert normalize_corpus_ids(corpus_id="", corpus_ids=[]) == []


def test_normalize_wraps_single_corpus_id_into_list():
    from utils.corpus_ids import normalize_corpus_ids

    assert normalize_corpus_ids(corpus_id="abc") == ["abc"]


def test_normalize_corpus_ids_takes_precedence_over_legacy_single():
    """When both fields are supplied, the new corpus_ids wins."""
    from utils.corpus_ids import normalize_corpus_ids

    out = normalize_corpus_ids(corpus_id="legacy", corpus_ids=["a", "b"])
    assert out == ["a", "b"]
    assert "legacy" not in out


def test_normalize_preserves_order_and_does_not_dedupe():
    from utils.corpus_ids import normalize_corpus_ids

    # Order matters for deterministic cache keys; dedup is the caller's job.
    assert normalize_corpus_ids(corpus_ids=["b", "a", "b"]) == ["b", "a", "b"]


def test_normalize_filters_falsy_elements_from_corpus_ids():
    from utils.corpus_ids import normalize_corpus_ids

    assert normalize_corpus_ids(corpus_ids=["a", "", "b", None]) == ["a", "b"]


def test_normalize_coerces_non_string_elements():
    from utils.corpus_ids import normalize_corpus_ids

    # Defensive: callers may pass UUID objects or other str-coercible types.
    assert normalize_corpus_ids(corpus_ids=[1, "two"]) == ["1", "two"]


def test_is_multi_corpus_disabled_reads_env_truthy_values():
    from utils.corpus_ids import is_multi_corpus_disabled

    for truthy in ("true", "TRUE", "True", "1", "yes", "on"):
        with _env("DISABLE_MULTI_CORPUS", truthy):
            assert is_multi_corpus_disabled() is True, f"expected truthy for {truthy!r}"


def test_is_multi_corpus_disabled_returns_false_for_unset_or_falsy():
    from utils.corpus_ids import is_multi_corpus_disabled

    for falsy in (None, "", "false", "0", "no", "off", "False"):
        with _env("DISABLE_MULTI_CORPUS", falsy):
            assert is_multi_corpus_disabled() is False, f"expected falsy for {falsy!r}"


def test_kill_switch_rejects_more_than_one_corpus():
    from utils.corpus_ids import (
        MultiCorpusDisabledError,
        normalize_corpus_ids,
    )

    with _env("DISABLE_MULTI_CORPUS", "true"):
        # Single id must still pass.
        assert normalize_corpus_ids(corpus_ids=["only"]) == ["only"]
        # Two ids must raise.
        with pytest.raises(MultiCorpusDisabledError) as exc_info:
            normalize_corpus_ids(corpus_ids=["a", "b"])
        # The error message must surface the flag name so operators can
        # diagnose it from a 400 response.
        assert "DISABLE_MULTI_CORPUS" in str(exc_info.value)


def test_kill_switch_inactive_when_unset_allows_many_corpora():
    from utils.corpus_ids import normalize_corpus_ids

    with _env("DISABLE_MULTI_CORPUS", None):
        assert normalize_corpus_ids(corpus_ids=["a", "b", "c"]) == ["a", "b", "c"]


# ─── compute_multi_corpus_signature ───────────────────────────────────────────


def test_signature_is_deterministic_and_order_independent():
    from utils.corpus_ids import compute_multi_corpus_signature

    a = compute_multi_corpus_signature({"alpha": "sig1", "beta": "sig2"})
    b = compute_multi_corpus_signature({"beta": "sig2", "alpha": "sig1"})
    assert a == b


def test_signature_changes_when_per_corpus_signature_changes():
    from utils.corpus_ids import compute_multi_corpus_signature

    a = compute_multi_corpus_signature({"alpha": "sig1"})
    b = compute_multi_corpus_signature({"alpha": "sig2"})
    assert a != b


def test_signature_changes_when_corpus_set_changes():
    from utils.corpus_ids import compute_multi_corpus_signature

    a = compute_multi_corpus_signature({"alpha": "sig1"})
    b = compute_multi_corpus_signature({"alpha": "sig1", "beta": "sig2"})
    assert a != b


def test_signature_accepts_iterable_of_tuples():
    from utils.corpus_ids import compute_multi_corpus_signature

    pairs = [("alpha", "sig1"), ("beta", "sig2")]
    assert compute_multi_corpus_signature(pairs) == compute_multi_corpus_signature(
        {"alpha": "sig1", "beta": "sig2"}
    )


# ─── Pydantic dual-field validators ───────────────────────────────────────────


def test_chat_request_accepts_more_than_three_corpora():
    """Chat and graph both accept the user's selected corpus scope.

    Retrieval budgets decide how much evidence is read; the schema should not
    reject a normal multi-corpus chat request just because it spans 4+ corpora.
    """
    from models.schemas import ChatRequest

    req = ChatRequest(
        message="compare these corpora",
        corpus_ids=["alpha", "beta", "gamma", "delta", "epsilon"],
    )
    assert req.corpus_ids == ["alpha", "beta", "gamma", "delta", "epsilon"]


def test_graph_discover_request_legacy_corpus_id_wraps_to_list():
    from models.schemas import GraphDiscoverRequest

    req = GraphDiscoverRequest(corpus_id="abc", query="hi")
    assert req.corpus_ids == ["abc"]
    assert req.corpus_id == "abc"  # legacy field preserved


def test_graph_discover_request_new_corpus_ids_passes_through():
    from models.schemas import GraphDiscoverRequest

    req = GraphDiscoverRequest(corpus_ids=["a", "b"], query="hi")
    assert req.corpus_ids == ["a", "b"]


def test_graph_discover_request_corpus_ids_wins_when_both_set():
    from models.schemas import GraphDiscoverRequest

    req = GraphDiscoverRequest(corpus_id="legacy", corpus_ids=["x"], query="hi")
    assert req.corpus_ids == ["x"]


def test_graph_query_request_legacy_corpus_id_wraps_to_list():
    from models.schemas import GraphQueryRequest

    req = GraphQueryRequest(corpus_id="abc", query="entity name")
    assert req.corpus_ids == ["abc"]


def test_graph_query_request_new_corpus_ids_passes_through():
    from models.schemas import GraphQueryRequest

    req = GraphQueryRequest(corpus_ids=["a", "b"], query="entity name")
    assert req.corpus_ids == ["a", "b"]


def test_graph_resume_candidate_request_legacy_corpus_id_wraps():
    from models.schemas import GraphResumeCandidateRequest

    req = GraphResumeCandidateRequest(corpus_id="abc", query="hi")
    assert req.corpus_ids == ["abc"]


def test_graph_discover_session_syncs_legacy_to_plural():
    from models.schemas import GraphDiscoverSession

    s = GraphDiscoverSession(session_id="s1", corpus_id="abc")
    assert s.corpus_ids == ["abc"]
    assert s.corpus_id == "abc"


def test_graph_discover_session_syncs_plural_to_legacy():
    """A session stored only with corpus_ids must echo the first id back as
    corpus_id so legacy clients reading the session keep working."""
    from models.schemas import GraphDiscoverSession

    s = GraphDiscoverSession(session_id="s1", corpus_ids=["a", "b"])
    assert s.corpus_ids == ["a", "b"]
    assert s.corpus_id == "a"


def test_graph_discover_response_has_corpus_ids_field():
    from models.schemas import GraphDiscoverResponse

    r = GraphDiscoverResponse(corpus_ids=["a", "b"])
    assert r.corpus_ids == ["a", "b"]
    # Default-empty stays as empty list, not None.
    r2 = GraphDiscoverResponse()
    assert r2.corpus_ids == []
    assert r2.web_evidence == {}


def test_graph_discover_response_preserves_auto_synthesis_web_evidence():
    from models.schemas import GraphDiscoverResponse

    payload = {
        "enabled": True,
        "fetch_depth": "snippets",
        "max_results": 2,
        "sources": [{"source_tier": "web_search", "url": "https://example.test"}],
    }
    r = GraphDiscoverResponse(
        auto_synthesis={"markdown": "answer", "web_evidence": payload},
        web_evidence=payload,
    )

    dumped = r.model_dump()
    assert dumped["web_evidence"]["enabled"] is True
    assert dumped["auto_synthesis"]["web_evidence"]["enabled"] is True


def test_graph_insight_packet_has_corpus_ids_field():
    from models.schemas import GraphInsightPacket

    p = GraphInsightPacket(query="q", corpus_ids=["a", "b"])
    assert p.corpus_ids == ["a", "b"]
    # Legacy single-corpus_id construction still works.
    p_legacy = GraphInsightPacket(query="q", corpus_id="legacy")
    assert p_legacy.corpus_id == "legacy"


def test_graph_discover_router_helpers_echo_scope_and_web_payload():
    from types import SimpleNamespace

    from routers.graph import _discover_result_corpus_ids, _discover_result_web_evidence

    result = SimpleNamespace(
        corpus_id="legacy",
        corpus_ids=["a", "b"],
        web_evidence={"enabled": True, "sources": [{"source_tier": "web_search"}]},
    )

    assert _discover_result_corpus_ids(result, ["fallback"]) == ["a", "b"]
    assert _discover_result_web_evidence(result)["enabled"] is True

    legacy = SimpleNamespace(corpus_id="legacy", corpus_ids=[])
    assert _discover_result_corpus_ids(legacy, ["fallback"]) == ["legacy"]


# ─── source_corpus on subtypes ────────────────────────────────────────────────


def test_context_graph_node_carries_source_attribution_with_safe_defaults():
    from models.schemas import ContextGraphNode

    n = ContextGraphNode(id="n1", label="Concept")
    assert n.source_corpus == ""
    assert n.source_corpora == []

    n2 = ContextGraphNode(
        id="n2", label="Concept", source_corpus="alpha", source_corpora=["alpha", "beta"]
    )
    assert n2.source_corpus == "alpha"
    assert n2.source_corpora == ["alpha", "beta"]


def test_context_graph_link_carries_source_and_dangling_defaults():
    from models.schemas import ContextGraphLink

    l = ContextGraphLink(source="a", target="b")
    assert l.source_corpus == ""
    assert l.source_corpora == []
    assert l.dangling is False

    l2 = ContextGraphLink(source="a", target="z", dangling=True, source_corpus="alpha")
    assert l2.dangling is True
    assert l2.source_corpus == "alpha"


def test_insight_packet_entity_carries_source_attribution():
    from models.schemas import GraphInsightPacketEntity

    e = GraphInsightPacketEntity(entity_id="e1", canonical_name="x")
    assert e.source_corpus == ""
    assert e.source_corpora == []


def test_insight_packet_edge_carries_source_attribution_and_dangling():
    from models.schemas import GraphInsightPacketEdge

    e = GraphInsightPacketEdge(source="a", target="b")
    assert e.source_corpus == ""
    assert e.source_corpora == []
    assert e.dangling is False


# ─── Backward compat — existing single-corpus call sites still work ──────────


def test_graph_discover_request_omits_corpus_ids_when_only_legacy_given():
    """The legacy router contract sent {corpus_id: str, query: str}. After
    PR 1 the same payload must still construct a valid request — the
    validator wraps corpus_id into corpus_ids automatically."""
    from models.schemas import GraphDiscoverRequest

    req = GraphDiscoverRequest.model_validate({"corpus_id": "x", "query": "hi"})
    assert req.corpus_id == "x"
    assert req.corpus_ids == ["x"]
    assert req.mode == "auto"


def test_graph_query_request_legacy_payload_still_validates():
    """The router at routers/graph.py:186 builds GraphQueryRequest from
    {corpus_id, query, max_hops, limit}. Verify the legacy payload still
    validates after the PR 1 schema swap."""
    from models.schemas import GraphQueryRequest

    req = GraphQueryRequest.model_validate(
        {"corpus_id": "x", "query": "neural network", "max_hops": 2, "limit": 50}
    )
    assert req.corpus_ids == ["x"]
    assert req.max_hops == 2
    assert req.limit == 50

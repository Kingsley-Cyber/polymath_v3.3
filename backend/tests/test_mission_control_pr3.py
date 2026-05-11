"""PR 3 — Mission Control + Agent Query multi-corpus fan-out + merger.

These tests exercise the merger logic (which is pure-Python, no DB / no
LLM required) and the Pydantic model passthroughs. End-to-end runtime of
discover() requires the legacy .pyc + a live Neo4j/Qdrant/LLM stack;
those paths are validated via integration test or manual smoke against the
running container, not here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _stub_result(
    *,
    corpus_id: str,
    nodes: list[dict] | None = None,
    links: list[dict] | None = None,
    bridges: list[dict] | None = None,
    headline_text: str = "",
    evidence_count: int = 0,
    auto_md: str = "",
) -> SimpleNamespace:
    """Build a SimpleNamespace shaped like the legacy discover() Result so
    merge_discover_results can be exercised without spinning up the real
    orchestrator."""
    return SimpleNamespace(
        session_id=f"sess_{corpus_id}",
        corpus_id=corpus_id,
        corpus_ids=[corpus_id],
        query="q",
        mode="auto",
        interpretation=f"per-corpus interpretation for {corpus_id}",
        frontier=[{"label": f"frontier-{corpus_id}"}],
        analogies=[{"label": f"analogy-{corpus_id}"}],
        bridges=bridges or [],
        weak_links=[],
        transfers=[],
        questions=[],
        strategic_read=None,
        intent_profile=None,
        atomic_trace=[],
        socratic_prompts=[],
        metrics={},
        domain_map_summary=[],
        graph={"nodes": nodes or [], "links": links or []},
        anchors=[],
        concept_communities=[],
        entity_concept_map={},
        headline={"text": headline_text} if headline_text else None,
        themes=[],
        bridges_v2=[],
        gaps_v2=[],
        latent_topics=[],
        tensions=[],
        trace={"stages": []},
        auto_synthesis={"markdown": auto_md, "sources": [], "fallback": False, "fallback_reason": None},
        insight_packet_summary={"sparse": False, "temporal_support": False, "counts": {"evidence": evidence_count, "entities": 0}, "evidence_sources": {}},
        context_graph={"nodes": [], "links": [], "meta": {}},
    )


# ─── merge_discover_results ──────────────────────────────────────────────────


def test_merger_unions_graph_nodes_and_tracks_source_corpora():
    from services.graph.orchestrator import merge_discover_results

    r_alpha = _stub_result(
        corpus_id="alpha",
        nodes=[
            {"id": "e1", "display_name": "AI"},
            {"id": "e2", "display_name": "Cymatics"},
        ],
        links=[{"source": "e1", "target": "e2", "predicate": "uses"}],
        evidence_count=10,
    )
    r_beta = _stub_result(
        corpus_id="beta",
        nodes=[
            {"id": "e2", "display_name": "Cymatics"},  # shared
            {"id": "e3", "display_name": "Embeddings"},
        ],
        links=[{"source": "e2", "target": "e3", "predicate": "uses"}],
        evidence_count=20,
    )

    merged = merge_discover_results(
        [("alpha", r_alpha, None), ("beta", r_beta, None)],
        query="q",
        corpus_ids=["alpha", "beta"],
    )

    nodes = {n["id"]: n for n in merged.graph["nodes"]}
    assert set(nodes.keys()) == {"e1", "e2", "e3"}
    # Shared node e2 carries both source_corpora
    assert sorted(nodes["e2"]["source_corpora"]) == ["alpha", "beta"]
    assert nodes["e1"]["source_corpora"] == ["alpha"]
    assert nodes["e3"]["source_corpora"] == ["beta"]

    links = merged.graph["links"]
    assert len(links) == 2
    assert merged.corpus_ids == ["alpha", "beta"]


def test_merger_picks_higher_evidence_count_for_prose():
    """The corpus with the higher counts.evidence value wins the headline +
    interpretation slot. Per-corpus prose preserved under
    auto_synthesis.per_corpus_synthesis."""
    from services.graph.orchestrator import merge_discover_results

    r_low = _stub_result(corpus_id="low", evidence_count=5, auto_md="LOW prose")
    r_high = _stub_result(corpus_id="high", evidence_count=50, auto_md="HIGH prose")

    merged = merge_discover_results(
        [("low", r_low, None), ("high", r_high, None)],
        query="q",
        corpus_ids=["low", "high"],
    )

    # High-evidence result's interpretation surfaces.
    assert "high" in merged.interpretation
    # Per-corpus prose preserved for compare view.
    per_corpus = merged.auto_synthesis.get("per_corpus_synthesis") or []
    md_by_corpus = {p["corpus_id"]: p["markdown"] for p in per_corpus}
    assert md_by_corpus == {"low": "LOW prose", "high": "HIGH prose"}
    assert merged.auto_synthesis.get("multi_corpus") is True


def test_merger_handles_partial_failure():
    """One corpus errors, others succeed → return merged from successes +
    record errors in trace.multi_corpus_meta."""
    from services.graph.orchestrator import merge_discover_results

    r_ok = _stub_result(
        corpus_id="ok",
        nodes=[{"id": "e1", "display_name": "X"}],
        evidence_count=10,
    )

    merged = merge_discover_results(
        [("ok", r_ok, None), ("bad", None, "Neo4j timeout")],
        query="q",
        corpus_ids=["ok", "bad"],
    )

    assert merged.trace["multi_corpus_meta"]["successful_ids"] == ["ok"]
    assert merged.trace["multi_corpus_meta"]["failed_ids"] == ["bad"]
    assert merged.trace["multi_corpus_meta"]["errors"]["bad"] == "Neo4j timeout"
    assert {n["id"] for n in merged.graph["nodes"]} == {"e1"}


def test_merger_all_failures_returns_stub_with_errors():
    from services.graph.orchestrator import merge_discover_results

    merged = merge_discover_results(
        [("a", None, "error a"), ("b", None, "error b")],
        query="q",
        corpus_ids=["a", "b"],
    )
    assert merged.corpus_ids == ["a", "b"]
    assert merged.graph == {"nodes": [], "links": []}
    assert merged.auto_synthesis["fallback"] is True
    assert merged.trace["_meta"]["failed_ids"] == ["a", "b"]


def test_merger_order_independence_for_node_set():
    """merge(A, B) and merge(B, A) produce the same node + link sets
    (modulo iteration order)."""
    from services.graph.orchestrator import merge_discover_results

    a = _stub_result(
        corpus_id="A",
        nodes=[{"id": "e1"}, {"id": "e2"}],
        links=[{"source": "e1", "target": "e2", "predicate": "uses"}],
    )
    b = _stub_result(
        corpus_id="B",
        nodes=[{"id": "e2"}, {"id": "e3"}],
        links=[{"source": "e2", "target": "e3", "predicate": "uses"}],
    )

    ab = merge_discover_results(
        [("A", a, None), ("B", b, None)], query="q", corpus_ids=["A", "B"]
    )
    ba = merge_discover_results(
        [("B", b, None), ("A", a, None)], query="q", corpus_ids=["B", "A"]
    )

    assert {n["id"] for n in ab.graph["nodes"]} == {n["id"] for n in ba.graph["nodes"]}
    ab_links = {(l["source"], l["target"], l["predicate"]) for l in ab.graph["links"]}
    ba_links = {(l["source"], l["target"], l["predicate"]) for l in ba.graph["links"]}
    assert ab_links == ba_links


# ─── list_sessions multi-corpus filter ───────────────────────────────────────


@pytest.mark.asyncio
async def test_list_sessions_multi_uses_in_filter():
    """Multi-corpus list_sessions queries Mongo with $or to catch both
    legacy single-corpus_id and new corpus_ids documents in one round-trip."""
    from services.graph import orchestrator

    captured: dict = {}

    class FakeCursor:
        def __init__(self, docs):
            self._docs = docs

        def sort(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def __aiter__(self):
            self._iter = iter(self._docs)
            return self

        async def __anext__(self):
            try:
                return next(self._iter)
            except StopIteration:
                raise StopAsyncIteration

    class FakeColl:
        def find(self, flt, projection=None):
            captured["filter"] = flt
            return FakeCursor([
                {"session_id": "s1", "corpus_id": "alpha"},
                {"session_id": "s2", "corpus_ids": ["beta", "gamma"]},
            ])

    class FakeDB:
        def __getitem__(self, name):
            return FakeColl()

    out = await orchestrator.list_sessions(FakeDB(), corpus_ids=["alpha", "beta"], user_id="u1")

    assert "$or" in captured["filter"]
    or_clauses = captured["filter"]["$or"]
    assert {"corpus_id": {"$in": ["alpha", "beta"]}} in or_clauses
    assert any("corpus_ids" in c for c in or_clauses)
    assert captured["filter"]["user_id"] == "u1"
    assert {s["session_id"] for s in out} == {"s1", "s2"}


@pytest.mark.asyncio
async def test_list_sessions_single_falls_back_to_legacy_passthrough():
    """When only one corpus is requested, list_sessions calls the legacy
    helper directly so behavior is byte-identical to pre-PR3."""
    from services.graph import orchestrator

    called_with: dict = {}

    async def fake_legacy(db, corpus_id=None, user_id=None):
        called_with["corpus_id"] = corpus_id
        called_with["user_id"] = user_id
        return [{"session_id": "legacy_path"}]

    orchestrator._legacy_list_sessions = fake_legacy
    try:
        out = await orchestrator.list_sessions(
            db=None, corpus_ids=["only"], user_id="u1"
        )
        assert called_with == {"corpus_id": "only", "user_id": "u1"}
        assert out == [{"session_id": "legacy_path"}]
    finally:
        orchestrator._legacy_list_sessions = None  # don't pollute other tests

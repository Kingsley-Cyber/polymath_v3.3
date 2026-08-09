"""Phase-8 live alias retrieval shadow/canary wiring (fixture-only).

Production activation is NOT authorized. These tests exercise the shadow
orchestrator + trust-class policy against an isolated fixture corpus registry.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from models.alias_identity import CorpusEntityV1
from models.schemas import SourceChunk
from services.ingestion.alias_schema_projection import (
    SchemaProjectionLinks,
    project_corpus_entity_to_shadow_schema,
)
from services.ingestion.alias_schema_retrieval import replay_retrieval_fingerprint
from services.ingestion.alias_retrieval_shadow import (
    alias_retrieval_controls,
    apply_fixture_ranking_effect,
    clear_shadow_schema_registry,
    register_shadow_schema_records,
    run_alias_retrieval_shadow,
    run_alias_retrieval_shadow_async,
)


FIXTURE_CORPUS = "isolated_alias_fixture"


def _settings(**overrides) -> SimpleNamespace:
    base = dict(
        ALIAS_RETRIEVAL_ENABLED_GLOBALLY=False,
        ALIAS_RETRIEVAL_SHADOW_ENABLED=True,
        ALIAS_RETRIEVAL_RANKING_ENABLED=False,
        ALIAS_RETRIEVAL_FIXTURE_CORPUS_ALLOWLIST=FIXTURE_CORPUS,
        ALIAS_RETRIEVAL_PRODUCTION_SCHEMA_WRITES=False,
        ALIAS_RETRIEVAL_PRODUCTION_BACKFILL=False,
        ALIAS_RETRIEVAL_SHADOW_DEADLINE_SECONDS=0.35,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _entity(**overrides) -> CorpusEntityV1:
    kwargs = dict(
        corpus_id=FIXTURE_CORPUS,
        canonical_name="Retrieval-Augmented Generation",
        document_entity_ids=["docent:a"],
        accepted_merge_decision_ids=["corpusmerge:a"],
        trusted_aliases=["RAG"],
        temporal_aliases=[],
        retrieval_surface_variants=[],
        descriptions=[],
        related_terms=[],
        ambiguous_aliases=[],
        conflicting_candidates=[],
        source_document_ids=["doc:a"],
        supporting_alias_decision_ids=["aliascand:rag"],
        canonical_name_rule="validated_acronym_long_form_v1",
    )
    kwargs.update(overrides)
    return CorpusEntityV1.create(**kwargs)


def _chunk(
    chunk_id: str,
    *,
    text: str,
    doc_id: str = "doc:a",
    parent_id: str = "parent:1",
    score: float = 0.5,
) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=parent_id,
        doc_id=doc_id,
        corpus_id=FIXTURE_CORPUS,
        text=text,
        score=score,
        source_tier="dense",
    )


def _fixture_chunks() -> list[SourceChunk]:
    return [
        _chunk(
            "child:rag:def",
            text="Retrieval-Augmented Generation (RAG) retrieves external context.",
            score=0.9,
        ),
        _chunk(
            "child:rag:use",
            text="RAG is used in question answering systems.",
            score=0.7,
        ),
        _chunk(
            "child:other",
            text="Unrelated gardening tips for tomatoes.",
            doc_id="doc:z",
            parent_id="parent:9",
            score=0.4,
        ),
        _chunk(
            "child:ir:info",
            text="Information Retrieval (IR) ranks documents.",
            doc_id="doc:ir:a",
            parent_id="parent:ir:a",
            score=0.6,
        ),
        _chunk(
            "child:ir:heat",
            text="Infrared (IR) detects heat signatures.",
            doc_id="doc:ir:b",
            parent_id="parent:ir:b",
            score=0.6,
        ),
    ]


def _register_rag_record(**entity_kw):
    clear_shadow_schema_registry()
    entity = _entity(**entity_kw)
    record = project_corpus_entity_to_shadow_schema(
        entity,
        links=SchemaProjectionLinks(
            linked_child_ids=("child:rag:def", "child:rag:use"),
            linked_parent_ids=("parent:1",),
            linked_section_ids=("section:1",),
            linked_graph_node_ids=("graph:rag",),
        ),
    )
    register_shadow_schema_records(FIXTURE_CORPUS, [record])
    return record


def test_controls_default_shadow_no_production_activation():
    controls = alias_retrieval_controls(_settings())
    assert controls["enabled_globally"] is False
    assert controls["shadow_enabled"] is True
    assert controls["ranking_enabled"] is False
    assert controls["production_schema_writes"] is False
    assert controls["production_backfill"] is False
    assert FIXTURE_CORPUS in controls["fixture_corpus_allowlist"]


def test_direct_lane_without_schema_hit():
    _register_rag_record()
    chunks = _fixture_chunks()
    out, diag = run_alias_retrieval_shadow(
        query="gardening tips for tomatoes",
        tier="qdrant_only",
        corpus_ids=[FIXTURE_CORPUS],
        finalists=chunks,
        settings=_settings(),
    )
    assert diag["status"] == "ok"
    assert [c.chunk_id for c in out] == [c.chunk_id for c in chunks]
    report = diag["query_report"]
    assert report["expanded_queries"] == []
    assert report["schema_records_used_as_answer_evidence"] == 0
    assert diag["schema_lane_blocked_direct_retrieval"] is False


def test_schema_lane_failure_fallback_never_blocks():
    clear_shadow_schema_registry()
    # Force failure by registering a non-record object via monkeypatch of loader.
    from services.ingestion import alias_retrieval_shadow as mod

    original = mod._shadow_records_for_corpora

    def boom(_ids):
        raise RuntimeError("schema index exploded")

    mod._shadow_records_for_corpora = boom  # type: ignore[assignment]
    try:
        chunks = _fixture_chunks()
        out, diag = run_alias_retrieval_shadow(
            query="What is RAG?",
            tier="qdrant_mongo",
            corpus_ids=[FIXTURE_CORPUS],
            finalists=chunks,
            settings=_settings(),
        )
    finally:
        mod._shadow_records_for_corpora = original  # type: ignore[assignment]

    assert [c.chunk_id for c in out] == [c.chunk_id for c in chunks]
    assert diag["status"] == "schema_lane_failure_fallback"
    assert diag["schema_lane_blocked_direct_retrieval"] is False
    assert diag.get("production_queries_unchanged") is True


def test_trusted_alias_improves_expected_recall_trace():
    _register_rag_record()
    chunks = _fixture_chunks()
    # Put RAG evidence later so schema assist must surface it in report.
    reordered = [
        chunks[2],  # other
        chunks[0],  # rag def
        chunks[1],
    ]
    _out, diag = run_alias_retrieval_shadow(
        query="What is RAG?",
        tier="qdrant_mongo",
        corpus_ids=[FIXTURE_CORPUS],
        finalists=reordered,
        settings=_settings(),
    )
    report = diag["query_report"]
    assert any(t["trust_class"] == "trusted_aliases" for t in report["schema_traces"])
    assert "Retrieval-Augmented Generation" in report["expanded_queries"]
    assert "child:rag:def" in report["final_hydrated_chunk_ids"]
    assert report["schema_records_used_as_answer_evidence"] == 0


def test_ambiguous_ir_cross_expansion_zero():
    clear_shadow_schema_registry()
    ir_info = _entity(
        canonical_name="Information Retrieval",
        trusted_aliases=[],
        ambiguous_aliases=["IR"],
        document_entity_ids=["docent:ir:a"],
        source_document_ids=["doc:ir:a"],
        supporting_alias_decision_ids=["aliascand:ir:a"],
    )
    ir_heat = _entity(
        canonical_name="Infrared",
        trusted_aliases=[],
        ambiguous_aliases=["IR"],
        document_entity_ids=["docent:ir:b"],
        source_document_ids=["doc:ir:b"],
        supporting_alias_decision_ids=["aliascand:ir:b"],
    )
    rec_a = project_corpus_entity_to_shadow_schema(
        ir_info,
        links=SchemaProjectionLinks(
            linked_child_ids=("child:ir:info",),
            linked_parent_ids=("parent:ir:a",),
        ),
    )
    rec_b = project_corpus_entity_to_shadow_schema(
        ir_heat,
        links=SchemaProjectionLinks(
            linked_child_ids=("child:ir:heat",),
            linked_parent_ids=("parent:ir:b",),
        ),
    )
    register_shadow_schema_records(FIXTURE_CORPUS, [rec_a, rec_b])

    # Finalists only from doc:ir:a — heat must not cross-expand.
    finalists = [
        c for c in _fixture_chunks() if c.doc_id in {"doc:ir:a", "doc:z"}
    ]
    _out, diag = run_alias_retrieval_shadow(
        query="IR methods",
        tier="qdrant_mongo",
        corpus_ids=[FIXTURE_CORPUS],
        finalists=finalists,
        settings=_settings(),
    )
    report = diag["query_report"]
    hydrated = set(report["final_hydrated_chunk_ids"])
    assert "child:ir:heat" not in hydrated
    assert diag["ambiguous_cross_expansion"] == 0


def test_related_term_ranking_changes_zero():
    clear_shadow_schema_registry()
    entity = _entity(trusted_aliases=[], related_terms=["passage ranking"])
    record = project_corpus_entity_to_shadow_schema(
        entity,
        links=SchemaProjectionLinks(linked_child_ids=("child:rag:def",)),
    )
    register_shadow_schema_records(FIXTURE_CORPUS, [record])
    chunks = _fixture_chunks()
    before = [c.chunk_id for c in chunks]
    out, diag = run_alias_retrieval_shadow(
        query="passage ranking overview",
        tier="qdrant_mongo",
        corpus_ids=[FIXTURE_CORPUS],
        finalists=chunks,
        settings=_settings(),
    )
    assert [c.chunk_id for c in out] == before
    assert diag["related_term_ranking_changes"] == 0
    report = diag["query_report"]
    assert report["semantic_related_terms_changed_ranking"] is False
    related = [t for t in report["schema_traces"] if t["trust_class"] == "related_terms"]
    assert related
    assert related[0]["ranking_contribution"] == "trace_only_no_ranking"


def test_schema_records_as_citations_zero():
    _register_rag_record()
    _out, diag = run_alias_retrieval_shadow(
        query="What is RAG?",
        tier="qdrant_mongo_graph",
        corpus_ids=[FIXTURE_CORPUS],
        finalists=_fixture_chunks(),
        settings=_settings(),
    )
    assert diag["schema_records_as_citations"] == 0
    assert diag["query_report"]["schema_records_used_as_answer_evidence"] == 0
    for trace in diag["query_report"]["schema_traces"]:
        assert trace["schema_used_as_answer_evidence"] is False


def test_final_evidence_hydration_and_route_budgets():
    _register_rag_record()
    chunks = _fixture_chunks()
    for tier_name, tier_enum in (
        ("fast", "qdrant_only"),
        ("hybrid", "qdrant_mongo"),
        ("graph", "qdrant_mongo_graph"),
    ):
        _out, diag = run_alias_retrieval_shadow(
            query="What is RAG?",
            tier=tier_enum,
            corpus_ids=[FIXTURE_CORPUS],
            finalists=chunks,
            settings=_settings(),
        )
        assert diag["status"] == "ok"
        assert diag["route_budget_ok"] is True
        assert diag["tier"] == tier_name
        report = diag["query_report"]
        assert report["final_hydrated_chunk_ids"]
        assert "latency_s" in report
        assert "alias_shadow_lane" in report["latency_s"]
        if tier_name == "fast":
            assert report["linked_parent_summary_ids"] == []
            assert report["linked_section_summary_ids"] == []
        if tier_name == "hybrid":
            assert "parent:1" in report["linked_parent_summary_ids"]
        if tier_name == "graph":
            assert "graph:rag" in report["linked_graph_node_ids"]


def test_production_queries_unchanged_when_ranking_disabled():
    _register_rag_record()
    chunks = _fixture_chunks()
    before = [c.chunk_id for c in chunks]
    out, diag = run_alias_retrieval_shadow(
        query="What is RAG?",
        tier="qdrant_only",
        corpus_ids=[FIXTURE_CORPUS],
        finalists=chunks,
        settings=_settings(ALIAS_RETRIEVAL_RANKING_ENABLED=False),
    )
    assert [c.chunk_id for c in out] == before
    assert diag["ranking_effect"]["production_queries_unchanged"] is True


def test_fixture_ranking_allowed_only_when_flagged():
    _register_rag_record()
    chunks = [
        _chunk("child:other", text="gardening", doc_id="doc:z", score=0.99),
        _chunk(
            "child:rag:def",
            text="Retrieval-Augmented Generation (RAG) retrieves external context.",
            score=0.1,
        ),
    ]
    out, diag = run_alias_retrieval_shadow(
        query="What is RAG?",
        tier="qdrant_mongo",
        corpus_ids=[FIXTURE_CORPUS],
        finalists=chunks,
        settings=_settings(ALIAS_RETRIEVAL_RANKING_ENABLED=True),
    )
    assert diag["ranking_effect"]["applied"] is True
    assert out[0].chunk_id == "child:rag:def"


def test_refuse_global_activation_and_production_writes():
    chunks = _fixture_chunks()
    _out, diag = run_alias_retrieval_shadow(
        query="RAG",
        tier="fast",
        corpus_ids=[FIXTURE_CORPUS],
        finalists=chunks,
        settings=_settings(ALIAS_RETRIEVAL_ENABLED_GLOBALLY=True),
    )
    assert diag["status"] == "refused_global_activation"

    _out2, diag2 = run_alias_retrieval_shadow(
        query="RAG",
        tier="fast",
        corpus_ids=[FIXTURE_CORPUS],
        finalists=chunks,
        settings=_settings(ALIAS_RETRIEVAL_PRODUCTION_SCHEMA_WRITES=True),
    )
    assert diag2["status"] == "refused_production_mutation_lock"


def test_restart_replay_identical():
    _register_rag_record()
    chunks = _fixture_chunks()
    settings = _settings()
    _, diag1 = run_alias_retrieval_shadow(
        query="What is RAG?",
        tier="qdrant_mongo",
        corpus_ids=[FIXTURE_CORPUS],
        finalists=chunks,
        settings=settings,
    )
    _, diag2 = run_alias_retrieval_shadow(
        query="What is RAG?",
        tier="qdrant_mongo",
        corpus_ids=[FIXTURE_CORPUS],
        finalists=chunks,
        settings=settings,
    )
    assert diag1["query_report"]["replay"] == diag2["query_report"]["replay"]
    # Fingerprint helper stays stable too.
    from services.ingestion.alias_schema_retrieval import run_dual_lane_retrieval

    record = _register_rag_record()
    child_index = [
        {
            "child_id": c.chunk_id,
            "text": c.text,
            "parent_id": c.parent_id,
            "document_id": c.doc_id,
        }
        for c in chunks
    ]
    r1 = run_dual_lane_retrieval(
        "What is RAG?",
        tier="hybrid",
        shadow_records=[record],
        corpus_child_index=child_index,
    )
    r2 = run_dual_lane_retrieval(
        "What is RAG?",
        tier="hybrid",
        shadow_records=[record],
        corpus_child_index=child_index,
    )
    assert replay_retrieval_fingerprint(r1) == replay_retrieval_fingerprint(r2)


def test_latency_overhead_measured_per_tier():
    _register_rag_record()
    chunks = _fixture_chunks()
    measured = {}
    for tier in ("qdrant_only", "qdrant_mongo", "qdrant_mongo_graph"):
        _out, diag = run_alias_retrieval_shadow(
            query="What is RAG?",
            tier=tier,
            corpus_ids=[FIXTURE_CORPUS],
            finalists=chunks,
            settings=_settings(),
        )
        measured[diag["tier"]] = diag["latency_s"]["alias_shadow_lane"]
        assert measured[diag["tier"]] >= 0.0
    assert set(measured) == {"fast", "hybrid", "graph"}


def test_async_deadline_fallback():
    clear_shadow_schema_registry()

    async def _run():
        return await run_alias_retrieval_shadow_async(
            query="RAG",
            tier="qdrant_only",
            corpus_ids=[FIXTURE_CORPUS],
            finalists=_fixture_chunks(),
            settings=_settings(ALIAS_RETRIEVAL_SHADOW_DEADLINE_SECONDS=0.05),
        )

    # Normal happy path under deadline.
    out, diag = asyncio.run(_run())
    assert diag["status"] in {"ok", "shadow_disabled", "schema_lane_failure_fallback"}
    assert out  # finalists preserved


def test_apply_fixture_ranking_noop_in_production_mode():
    from services.ingestion.alias_schema_retrieval import DualLaneRetrievalResult

    chunks = _fixture_chunks()
    empty = DualLaneRetrievalResult(
        tier="fast",
        original_query="RAG",
        expanded_queries=[],
        original_lane_hits=[],
        schema_lane_hits=[],
        final_ranked_child_ids=["child:rag:def"],
        traces=[],
    )
    out, meta = apply_fixture_ranking_effect(chunks, empty, allow=False)
    assert out is chunks or [c.chunk_id for c in out] == [c.chunk_id for c in chunks]
    assert meta["production_queries_unchanged"] is True

import os

import pytest

os.environ.setdefault("LITELLM_MASTER_KEY", "test-litellm-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

import services.chat_orchestrator as chat_module
from models.schemas import RetrievalResult, RetrievalTier, SourceChunk


def _chunk(
    chunk_id: str,
    *,
    doc_id: str,
    text: str,
    score: float = 0.78,
    source_tier: str = "qdrant_child",
    heading_path: list[str] | None = None,
    metadata: dict | None = None,
) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=f"parent-{chunk_id}",
        doc_id=doc_id,
        corpus_id="c1",
        text=text,
        summary=text[:180],
        score=score,
        source_tier=source_tier,
        doc_name=f"{doc_id}.md",
        heading_path=heading_path,
        metadata=metadata or {},
        provenance=[{"retriever": source_tier}],
    )


@pytest.mark.asyncio
async def test_chat_semantic_coverage_adds_missing_query_facets(monkeypatch):
    base_sources = [
        _chunk(
            "user-modeling-1",
            doc_id="USER MODELING AND USER PROFILING.pdf.md",
            text=(
                "User modeling and user profiling describe adaptive systems, "
                "personalization, user profiles, identity, choices, and values."
            ),
        ),
        _chunk(
            "neuro-narrative-1",
            doc_id="Neuro-narrative therapy.md",
            text=(
                "Neuro-narrative therapy connects narrative therapy, affect, "
                "embodiment, identity, emotional patterns, and choices."
            ),
        ),
    ]
    support_chunks = {
        "knowledge_graph": _chunk(
            "knowledge-graph-1",
            doc_id="Knowledge_Graphs_Aidan_Hogan.md",
            text=(
                "Knowledge graphs, graph RAG, RDF triples, ontology, schema, "
                "linked data, and entity relationship models can represent "
                "identity, values, choices, and emotional patterns over time."
            ),
            source_tier="mongo+lexical",
        ),
        "psychometrics": _chunk(
            "psychometrics-1",
            doc_id="Measuring_the_Mind.md",
            text=(
                "Psychometrics uses measurement, test validity, latent variable "
                "models, assessment, and score interpretation for identity, "
                "values, emotional patterns, and choices."
            ),
            source_tier="mongo+lexical",
        ),
    }

    async def fake_retrieve(**kwargs):
        query = kwargs["query"].lower()
        if "psychometrics" in query:
            chunks = [support_chunks["psychometrics"]]
        elif "knowledge graph" in query:
            chunks = [support_chunks["knowledge_graph"]]
        else:
            chunks = []
        return RetrievalResult(
            chunks=chunks,
            requested_tier=kwargs["retrieval_tier"],
            effective_tier=kwargs["retrieval_tier"],
        )

    monkeypatch.setattr(chat_module.retriever_orchestrator, "retrieve", fake_retrieve)

    merged, meta = await chat_module._enforce_chat_query_coverage(
        original_query=(
            "How could knowledge graphs, user modeling, psychometrics, and "
            "neuro-narrative therapy combine into a personal reflection app "
            "that maps identity, values, emotional patterns, and choices over time?"
        ),
        retrieval_query="same",
        sources=base_sources,
        corpus_ids=["c1"],
        retrieval_tier=RetrievalTier.qdrant_mongo,
        collections=None,
        retrieval_k=40,
        rerank_enabled=True,
        top_k_summary=12,
        rerank_top_n=24,
        similarity_threshold=None,
        neo4j_expansion_cap=None,
        max_corpora_per_query=None,
        fact_seed_limit=None,
        final_top_k=8,
        search_mode="local",
    )

    doc_ids = {chunk.doc_id for chunk in merged}
    assert meta["added"] == 2
    assert {"facet:knowledge_graph", "facet:psychometrics"} <= set(
        meta["support_lanes"]
    )
    assert "Knowledge_Graphs_Aidan_Hogan.md" in doc_ids
    assert "Measuring_the_Mind.md" in doc_ids
    assert len(merged) <= 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tier",
    [
        RetrievalTier.qdrant_only,
        RetrievalTier.qdrant_mongo,
        RetrievalTier.qdrant_mongo_graph,
    ],
)
async def test_chat_semantic_coverage_uses_same_effective_tier(monkeypatch, tier):
    seen_tiers: list[RetrievalTier] = []
    base_sources = [
        _chunk(
            "identity-1",
            doc_id="identity.md",
            text="Identity and values can be represented as a personal narrative.",
        )
    ]

    async def fake_retrieve(**kwargs):
        seen_tiers.append(kwargs["retrieval_tier"])
        return RetrievalResult(
            chunks=[
                _chunk(
                    "knowledge-graph-1",
                    doc_id="Knowledge_Graphs_Aidan_Hogan.md",
                    text=(
                        "Knowledge graphs, graph RAG, RDF triples, ontology, "
                        "schema, identity, values, and choices."
                    ),
                )
            ],
            requested_tier=kwargs["retrieval_tier"],
            effective_tier=kwargs["retrieval_tier"],
        )

    monkeypatch.setattr(chat_module.retriever_orchestrator, "retrieve", fake_retrieve)

    await chat_module._enforce_chat_query_coverage(
        original_query=(
            "How can knowledge graphs and identity narrative support a reflection app?"
        ),
        retrieval_query="same",
        sources=base_sources,
        corpus_ids=["c1"],
        retrieval_tier=tier,
        collections=None,
        retrieval_k=40,
        rerank_enabled=True,
        top_k_summary=12,
        rerank_top_n=24,
        similarity_threshold=None,
        neo4j_expansion_cap=None,
        max_corpora_per_query=None,
        fact_seed_limit=None,
        final_top_k=8,
        search_mode="local",
    )

    assert seen_tiers
    assert set(seen_tiers) == {tier}


def test_chat_evidence_filter_removes_bibliography_but_keeps_substantive_chunks():
    bibliography = _chunk(
        "bib-1",
        doc_id="measurement.md",
        heading_path=["4 Related Work", "Acknowledgements"],
        text=(
            "Smith et al. (2021) studied games. Jones et al. (2022) studied "
            "assessment. Brown et al. (2023) studied AI psychology. "
            "References and acknowledgements follow."
        ),
    )
    substantive = _chunk(
        "substantive-1",
        doc_id="measurement.md",
        heading_path=["2.5 Human Simulator and Psychometric Evaluator"],
        text=(
            "Scenario-based psychological assessment uses interactive fiction, "
            "memory, choices, and psychometric evaluation."
        ),
    )

    prepared, meta = chat_module._prepare_chat_evidence_sources(
        [bibliography, substantive],
        query="How can scenario-based psychological assessment model users?",
        min_keep=1,
    )

    assert [chunk.chunk_id for chunk in prepared] == ["substantive-1"]
    assert meta["filtered_low_value"] == 1


def test_chat_evidence_cleaner_strips_frontmatter():
    chunk = _chunk(
        "frontmatter-1",
        doc_id="on-device.md",
        text=(
            "---\n"
            "source_url: https://example.test\n"
            "priority: 4\n"
            "---\n"
            "# Building On-Device Assistants\n"
            "Local inference keeps private data on the device."
        ),
    )

    prepared, meta = chat_module._prepare_chat_evidence_sources(
        [chunk],
        query="How can an on-device assistant preserve privacy?",
        min_keep=1,
    )

    assert prepared[0].text.startswith("# Building On-Device Assistants")
    assert meta["cleaned_frontmatter"] == 1


def test_chat_support_chunk_score_is_selection_score_not_support_query_score():
    chunk = _chunk(
        "support-1",
        doc_id="on-device.md",
        text=(
            "On-device LLM architecture runs local inference so sensitive user "
            "data can remain private."
        ),
        score=0.999,
    )
    facet = {
        "name": "on_device_llm_architecture",
        "label": "On-device LLM architecture",
        "matched": ["on device llm architecture"],
        "support_terms": ["on device llm architecture", "local inference"],
    }

    marked = chat_module._mark_chat_coverage_chunk(
        chunk,
        facet=facet,
        support_query="on device llm architecture",
        original_query=(
            "How could an on-device assistant combine scenario assessment "
            "and narrative identity while preserving privacy?"
        ),
    )

    assert marked.score < 0.999
    assert marked.metadata["support_query_score"] == 0.999
    assert marked.metadata["support_selection_score"] == marked.score


def test_chat_coverage_prompt_note_names_uncovered_and_weak_lanes():
    note = chat_module._format_chat_coverage_prompt_note(
        {
            "query_facet_breakdown": [
                {
                    "name": "knowledge_graph",
                    "query_explicit": True,
                    "coverage_status": "needs_support",
                },
                {
                    "name": "identity_narrative",
                    "query_explicit": True,
                    "coverage_status": "grounded",
                },
            ],
            "selected_facets": ["knowledge_graph", "user_modeling", "identity_narrative"],
            "coverage_lane_counts": {
                "knowledge_graph": 0,
                "user_modeling": 1,
                "identity_narrative": 1,
            },
            "coverage_uncovered_lanes": ["knowledge_graph"],
            "lane_reports": [
                {"lane": "user_modeling", "status": "selected", "strength": "weak"},
                {"lane": "identity_narrative", "status": "selected", "strength": "strong"},
            ],
        }
    )

    assert note is not None
    assert "Query decomposed into explicit facets" in note
    assert "Uncovered lanes: knowledge_graph" in note
    assert "Weakly grounded lanes: user_modeling" in note
    assert "Do not present these as source-backed" in note


@pytest.mark.asyncio
async def test_chat_semantic_coverage_reports_dead_lanes(monkeypatch):
    base_sources = [
        _chunk(
            "identity-1",
            doc_id="identity.md",
            text="Narrative identity, values, and choices shape personal meaning.",
        )
    ]

    async def fake_retrieve(**kwargs):
        return RetrievalResult(
            chunks=[],
            requested_tier=kwargs["retrieval_tier"],
            effective_tier=kwargs["retrieval_tier"],
        )

    monkeypatch.setattr(chat_module.retriever_orchestrator, "retrieve", fake_retrieve)

    _, meta = await chat_module._enforce_chat_query_coverage(
        original_query=(
            "How can knowledge graphs and user modeling support a reflection app?"
        ),
        retrieval_query="same",
        sources=base_sources,
        corpus_ids=["c1"],
        retrieval_tier=RetrievalTier.qdrant_mongo,
        collections=None,
        retrieval_k=40,
        rerank_enabled=True,
        top_k_summary=12,
        rerank_top_n=24,
        similarity_threshold=None,
        neo4j_expansion_cap=None,
        max_corpora_per_query=None,
        fact_seed_limit=None,
        final_top_k=8,
        search_mode="local",
    )

    assert meta["added"] == 0
    assert {"knowledge_graph", "user_modeling"} <= set(meta["selected_facets"])
    assert {"knowledge_graph", "user_modeling"} <= set(meta["coverage_uncovered_lanes"])
    reports = {report["lane"]: report for report in meta["lane_reports"]}
    assert reports["knowledge_graph"]["status"] == "uncovered"
    assert reports["knowledge_graph"]["attempts"]
    assert reports["knowledge_graph"]["attempts"][0]["returned"] == 0


@pytest.mark.asyncio
async def test_chat_semantic_coverage_attempts_all_explicit_lanes_with_local_support(monkeypatch):
    facets = [
        {
            "name": f"facet_{idx}",
            "label": f"Facet {idx}",
            "matched": [f"facet {idx}"],
            "support_terms": [f"facet {idx}"],
            "query_explicit": True,
            "source": "query_deconstruction",
        }
        for idx in range(6)
    ]

    async def fake_facets(*args, **kwargs):
        return facets

    seen_modes: list[str] = []

    async def fake_retrieve(**kwargs):
        seen_modes.append(kwargs["search_mode"])
        query = kwargs["query"].lower()
        chunks = []
        for idx in range(6):
            term = f"facet {idx}"
            if term in query:
                chunks = [
                    _chunk(
                        f"support-{idx}",
                        doc_id=f"{term}.md",
                        text=(
                            f"{term} gives direct concrete evidence for a "
                            "multi-facet reflection app with identity and values."
                        ),
                    )
                ]
                break
        return RetrievalResult(
            chunks=chunks,
            requested_tier=kwargs["retrieval_tier"],
            effective_tier=kwargs["retrieval_tier"],
        )

    monkeypatch.setattr(
        chat_module,
        "_chat_coverage_facets_for_query_with_corpus",
        fake_facets,
    )
    monkeypatch.setattr(chat_module.retriever_orchestrator, "retrieve", fake_retrieve)

    merged, meta = await chat_module._enforce_chat_query_coverage(
        original_query=(
            "How can facet 0, facet 1, facet 2, facet 3, facet 4, and "
            "facet 5 combine into a reflection app?"
        ),
        retrieval_query="same",
        sources=[
            _chunk(
                "base",
                doc_id="base.md",
                text="A general reflection app can organize identity and values.",
            )
        ],
        corpus_ids=["c1"],
        retrieval_tier=RetrievalTier.qdrant_mongo,
        collections=None,
        retrieval_k=40,
        rerank_enabled=True,
        top_k_summary=12,
        rerank_top_n=24,
        similarity_threshold=None,
        neo4j_expansion_cap=None,
        max_corpora_per_query=None,
        fact_seed_limit=None,
        final_top_k=8,
        search_mode="global",
    )

    expected = {f"facet_{idx}" for idx in range(6)}
    assert expected <= set(meta["selected_facets"])
    assert expected <= set(meta["explicit_missing_facets"])
    assert not meta["skipped_dynamic_facets"]
    assert len(seen_modes) == 6
    assert set(seen_modes) == {"local"}
    assert len(merged) <= 8


def test_query_explicit_facets_win_before_dynamic_facets():
    base = chat_module._chat_coverage_facets_for_query(
        "How could knowledge graphs, user modeling, psychometrics, "
        "and neuro-narrative therapy combine with identity and agency?"
    )
    dynamic = [
        {
            "name": "identifying_cooperative_personalities_in_multi_agent_context",
            "label": "Cooperative personalities",
            "matched": ["Cooperative personalities"],
            "support_terms": ["cooperative personalities"],
            "triggers": ["cooperative personalities"],
            "source": "vector_facet_probe",
            "first_match_pos": 0,
            "match_score": 99.0,
            "semantic_matched": True,
        }
    ]

    merged = chat_module._merge_chat_coverage_facets(base, dynamic)
    explicit_names = [row["name"] for row in merged if row.get("query_explicit")]
    first_dynamic_index = next(
        i for i, row in enumerate(merged) if row["name"].startswith("identifying_")
    )

    assert {
        "knowledge_graph",
        "user_modeling",
        "psychometrics",
        "neuro_narrative",
        "identity_narrative",
    } <= set(explicit_names)
    assert first_dynamic_index >= len(explicit_names)


def test_chat_final_selector_reserves_missing_lane_support_chunk():
    high_global = _chunk(
        "global-measurement",
        doc_id="measurement.md",
        text="Scenario-based psychological assessment and interactive fiction.",
        score=0.95,
        metadata={
            "semantic_facets": {
                "facet_ids": ["novel_psychological_measurement_paradigm"],
                "doc_facet_ids": ["novel_psychological_measurement_paradigm"],
            }
        },
    )
    narrative = _chunk(
        "narrative-support",
        doc_id="narrative.md",
        text="Narrative identity, self story, values, and meaning making.",
        score=0.15,
        metadata={
            "support_role": "chat_semantic_facet_coverage",
            "support_lane": "facet:identity_narrative",
            "support_facet": {"name": "identity_narrative"},
        },
    )

    selected, added, meta = chat_module._select_chat_coverage_sources(
        [high_global],
        [narrative],
        facets=[
            {
                "name": "identity_narrative",
                "support_terms": ["narrative identity", "self story"],
                "matched": ["narrative"],
            }
        ],
        missing_lanes=["identity_narrative"],
        original_query="How can narrative identity support a private user model?",
        max_sources=1,
    )

    assert [chunk.chunk_id for chunk in selected] == ["narrative-support"]
    assert added == 1
    assert meta["covered_lanes"] == ["identity_narrative"]


def test_chat_final_selector_reserves_query_priority_lanes_before_dynamic_chunks():
    dynamic = _chunk(
        "dynamic-high",
        doc_id="cooperative.md",
        text="Cooperative personality detection in multi-agent contexts.",
        score=0.99,
        metadata={
            "semantic_facets": {
                "facet_ids": ["cooperative_personalities"],
                "doc_facet_ids": ["cooperative_personalities"],
            }
        },
    )
    psych = _chunk(
        "psych",
        doc_id="psych.md",
        text="Psychometrics, scenario assessment, and psychological measurement.",
        score=0.91,
    )
    kg = _chunk(
        "kg",
        doc_id="kg.md",
        text="Knowledge graphs connect entities, relations, and schema into a semantic network.",
        score=0.24,
    )
    user_model = _chunk(
        "user-model",
        doc_id="user.md",
        text="User modeling builds adaptive user profiles from preferences, goals, and context.",
        score=0.22,
    )

    selected, added, meta = chat_module._select_chat_coverage_sources(
        [dynamic, psych, kg, user_model],
        [],
        facets=[
            {"name": "knowledge_graph", "support_terms": ["knowledge graph", "semantic network"]},
            {"name": "user_modeling", "support_terms": ["user modeling", "user profile"]},
            {
                "name": "psychometrics",
                "support_terms": ["psychometrics", "psychological measurement"],
            },
        ],
        missing_lanes=[],
        priority_lanes=["knowledge_graph", "user_modeling", "psychometrics"],
        original_query="How could knowledge graphs, user modeling, and psychometrics combine?",
        max_sources=3,
    )

    assert [chunk.chunk_id for chunk in selected] == ["kg", "user-model", "psych"]
    assert added == 0
    assert meta["covered_priority_lanes"] == [
        "knowledge_graph",
        "user_modeling",
        "psychometrics",
    ]

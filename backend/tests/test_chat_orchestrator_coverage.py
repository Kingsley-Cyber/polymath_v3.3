import os

import pytest

os.environ.setdefault("LITELLM_MASTER_KEY", "test-litellm-master-key")
os.environ.setdefault("AUTH_SECRET_KEY", "test-auth-secret-key")
os.environ.setdefault("DEFAULT_ADMIN_PASSWORD", "test-admin-password")

import services.chat_orchestrator as chat_module
from services.facets.runtime import matching_ingest_facets
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


class _FakeFacetCursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return list(self.rows)


class _FakeFacetCollection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, *args, **kwargs):
        return _FakeFacetCursor(self.rows)


class _FakeFacetDb:
    def __init__(self, rows):
        self.rows = rows

    def __getitem__(self, name):
        assert name == "documents"
        return _FakeFacetCollection(self.rows)


@pytest.mark.asyncio
async def test_named_ingest_facets_are_query_explicit():
    rows = await matching_ingest_facets(
        _FakeFacetDb(
            [
                {
                    "doc_id": "cooperative-doc",
                    "corpus_id": "c1",
                    "filename": "Identifying_Cooperative_Personalities.md",
                    "facet_profile": {
                        "doc_facets": [
                            {
                                "facet_id": "cooperative_personality",
                                "display_name": "Cooperative Personality",
                                "aliases": ["team roles", "multi-agent cooperation"],
                                "search_terms": ["cooperative personality"],
                            },
                            {
                                "facet_id": "interpersonal_perception",
                                "display_name": "Interpersonal Perception",
                                "aliases": ["person perception", "perceiving others"],
                                "search_terms": ["interpersonal perception"],
                            },
                        ]
                    },
                }
            ]
        ),
        (
            "How could cooperative personality and interpersonal perception "
            "shape a personal reflection app?"
        ),
        ["c1"],
    )
    by_name = {row["name"]: row for row in rows}

    assert by_name["cooperative_personality"]["query_explicit"] is True
    assert by_name["interpersonal_perception"]["query_explicit"] is True


@pytest.mark.asyncio
async def test_generic_framework_token_does_not_trigger_unrelated_ingest_facet():
    db = _FakeFacetDb(
        [
            {
                "doc_id": "security-doc",
                "corpus_id": "c1",
                "filename": "Building Secure and Reliable Systems.md",
                "facet_profile": {
                    "doc_facets": [
                        {
                            "facet_id": "frameworks_enforce_security",
                            "display_name": "Frameworks Enforce Security",
                            "aliases": ["security frameworks", "secure systems"],
                            "search_terms": ["frameworks enforce security"],
                        }
                    ]
                },
            },
            {
                "doc_id": "personality-doc",
                "corpus_id": "c1",
                "filename": "The Handbook of Personality Assessment.md",
                "facet_profile": {
                    "doc_facets": [
                        {
                            "facet_id": "personality_assessment",
                            "display_name": "Personality Assessment",
                            "aliases": ["personality tests", "personality framework"],
                            "search_terms": ["personality assessment"],
                        }
                    ]
                },
            },
        ]
    )

    rows = await matching_ingest_facets(
        db,
        "How do personality frameworks relate to seduction tactics?",
        ["c1"],
    )

    names = {row["name"] for row in rows}
    assert "personality_assessment" in names
    assert "frameworks_enforce_security" not in names


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
async def test_chat_semantic_coverage_support_tier_downgrades_graph_only(monkeypatch, tier):
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
    # Coverage uses the main tier EXCEPT graph, which downgrades to hybrid for
    # the support retrievals (gap-fill chunks don't need Neo4j graph expansion;
    # the hybrid `naive` collection is a superset of the graph collection).
    expected = (
        RetrievalTier.qdrant_mongo
        if tier == RetrievalTier.qdrant_mongo_graph
        else tier
    )
    assert set(seen_tiers) == {expected}


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


def test_final_context_answerability_gate_off_is_noop():
    sources = [
        _chunk(
            "noise-1",
            doc_id="appendix.md",
            text="Appendix permissions, illustration credits, and page numbers.",
        )
    ]

    gated, meta = chat_module._apply_final_context_answerability_gate(
        sources,
        query="How do personality frameworks relate to seduction tactics?",
        mode="off",
    )

    assert [chunk.chunk_id for chunk in gated] == ["noise-1"]
    assert meta["enabled"] is False
    assert meta["dropped"] == 0


def test_final_context_answerability_gate_soft_drops_non_answering_chunks():
    query = "How do personality frameworks relate to seduction tactics?"
    plan = chat_module.build_evidence_plan(query)
    sources = [
        _chunk(
            "noise-1",
            doc_id="appendix.md",
            text="Appendix permissions, illustration credits, and page numbers.",
        ),
        _chunk(
            "seduction-1",
            doc_id="art-of-seduction.md",
            text=(
                "Seduction tactics create desire by reading vulnerability and "
                "adopting a role such as the ideal lover."
            ),
        ),
        _chunk(
            "personality-1",
            doc_id="four-tendencies.md",
            text=(
                "Personality frameworks such as the Big Five and the Four "
                "Tendencies describe stable patterns in motivation."
            ),
        ),
        _chunk(
            "reserved-personality",
            doc_id="handbook-personality.md",
            text="Obligers respond to outer expectations more than inner expectations.",
            metadata={"support_role": "evidence_plan_lane"},
        ),
    ]

    gated, meta = chat_module._apply_final_context_answerability_gate(
        sources,
        query=query,
        evidence_plan=plan,
        mode="soft",
        min_keep=2,
    )

    assert [chunk.chunk_id for chunk in gated] == [
        "seduction-1",
        "personality-1",
        "reserved-personality",
    ]
    assert meta["enabled"] is True
    assert meta["dropped"] == 1
    assert meta["dropped_chunk_ids"] == ["noise-1"]
    by_id = {chunk.chunk_id: chunk for chunk in gated}
    assert (
        by_id["reserved-personality"].metadata["answerability_chunk_gate"]["reason"]
        == "reserved_support"
    )


def test_final_context_answerability_gate_ignores_generic_modifier_terms():
    query = "Which personality types are vulnerable to seduction tactics?"
    plan = chat_module.build_evidence_plan(query)
    sources = [
        _chunk(
            "typescript-1",
            doc_id="programming-typescript.md",
            text=(
                "TypeScript uses structural types, generic frameworks, and "
                "implementation tactics for safer APIs."
            ),
        ),
        _chunk(
            "seduction-1",
            doc_id="art-of-seduction.md",
            text="Seduction exploits vulnerability by shaping attention and desire.",
        ),
        _chunk(
            "personality-1",
            doc_id="personality-assessment.md",
            text="Personality assessment describes stable traits and motivations.",
        ),
    ]

    gated, meta = chat_module._apply_final_context_answerability_gate(
        sources,
        query=query,
        evidence_plan=plan,
        mode="soft",
        min_keep=2,
    )

    assert [chunk.chunk_id for chunk in gated] == ["seduction-1", "personality-1"]
    assert meta["dropped_chunk_ids"] == ["typescript-1"]


def test_seduction_body_personality_mention_does_not_cover_personality_lane():
    plan = chat_module.build_evidence_plan(
        "How do personality frameworks relate to the Art of Seduction tactics?"
    )
    personality_lane = next(
        lane for lane in plan.required_lanes if lane.name == "personality_framework"
    )
    seduction_chunk = _chunk(
        "seduction-personality",
        doc_id="The Art of Seduction.md",
        text=(
            "A seductive personality can create desire, but this passage does "
            "not describe a psychological instrument or trait model."
        ),
    )
    personality_chunk = _chunk(
        "personality-assessment",
        doc_id="The Handbook of Personality Assessment.md",
        text="Personality assessment compares inventories, traits, and test validity.",
    )

    assert (
        chat_module._evidence_lane_match_score(seduction_chunk, personality_lane)
        < chat_module._lane_strong_score()
    )
    assert (
        chat_module._evidence_lane_match_score(personality_chunk, personality_lane)
        >= chat_module._lane_strong_score()
    )


def test_personality_query_grounding_alone_does_not_cover_personality_lane():
    query = "Give me the full spectrum across all personality books and Art of Seduction."
    plan = chat_module.build_evidence_plan(query)
    personality_lane = next(
        lane for lane in plan.required_lanes if lane.name == "personality"
    )
    software_chunk = _chunk(
        "software-grounded-personality",
        doc_id="software-architecture.md",
        text="Cross-cutting forces act across architecture dimensions and service boundaries.",
        metadata={"query_grounding": {"matched": ["personality"]}},
    )

    assert (
        chat_module._evidence_lane_match_score(software_chunk, personality_lane)
        < chat_module._lane_strong_score()
    )

    gated, meta = chat_module._apply_final_context_answerability_gate(
        [software_chunk],
        query=query,
        evidence_plan=plan,
        mode="soft",
        min_keep=1,
    )

    assert gated == []
    assert meta["dropped_chunk_ids"] == ["software-grounded-personality"]


def test_evidence_lane_support_can_deepen_existing_single_source_lane():
    plan = chat_module.build_evidence_plan(
        "Give me the full spectrum across personality books and Art of Seduction."
    )
    seduction_lane = next(lane for lane in plan.required_lanes if lane.name == "seduction")
    candidates = [
        _chunk(
            "art-2",
            doc_id="art-of-seduction",
            text="The seducer creates desire through attention and suggestion.",
            score=0.9,
        ),
        _chunk(
            "software-1",
            doc_id="software-patterns",
            text="Design patterns coordinate services across modules.",
            score=0.95,
        ),
    ]

    picks, report = chat_module._choose_evidence_lane_candidates_with_report(
        candidates,
        lane=seduction_lane,
        original_query="Give me the full spectrum across personality books and Art of Seduction.",
        existing_chunk_ids={"art-1"},
        existing_doc_ids={"art-of-seduction"},
        semantic_doc_ids=set(),
        target_k=1,
        same_doc_target_k=1,
    )

    assert [chunk.chunk_id for chunk in picks] == ["art-2"]
    assert report["reason"] == "same_doc_deepening_after_new_doc_exhausted"


def test_final_context_answerability_gate_demotes_to_honor_min_keep():
    query = "How do personality frameworks relate to seduction tactics?"
    sources = [
        _chunk(
            "noise-before",
            doc_id="appendix-a.md",
            text="Appendix permissions, illustration credits, and page numbers.",
        ),
        _chunk(
            "seduction-1",
            doc_id="art-of-seduction.md",
            text="Seduction tactics create desire by reading vulnerability.",
        ),
        _chunk(
            "noise-after",
            doc_id="appendix-b.md",
            text="Publication history and printing information.",
        ),
    ]

    gated, meta = chat_module._apply_final_context_answerability_gate(
        sources,
        query=query,
        mode="soft",
        min_keep=3,
    )

    # Non-answering chunks survive only because the minimum context floor would
    # otherwise be violated, and they move behind the answer-bearing chunk.
    assert [chunk.chunk_id for chunk in gated] == [
        "seduction-1",
        "noise-before",
        "noise-after",
    ]
    assert meta["dropped"] == 0
    assert meta["demoted"] == 2
    assert {
        chunk.metadata["answerability_chunk_gate"]["status"]
        for chunk in gated
        if chunk.chunk_id.startswith("noise")
    } == {"demoted_min_keep"}


def test_final_context_answerability_gate_filters_full_spectrum_off_domain_chunks():
    query = "Give me the full spectrum across all personality books and Art of Seduction."
    plan = chat_module.build_evidence_plan(query)
    sources = [
        _chunk(
            "personality-1",
            doc_id="personality-assessment.md",
            text="Personality assessment compares stable traits and inventories.",
        ),
        _chunk(
            "seduction-1",
            doc_id="art-of-seduction.md",
            text="Seduction shapes attention through desire and vulnerability.",
        ),
        _chunk(
            "software-1",
            doc_id="software-patterns.md",
            text="Implementation patterns coordinate services across a system.",
        ),
    ]

    gated, meta = chat_module._apply_final_context_answerability_gate(
        sources,
        query=query,
        evidence_plan=plan,
        mode="soft",
        min_keep=3,
    )

    assert [chunk.chunk_id for chunk in gated] == ["personality-1", "seduction-1"]
    assert meta["enabled"] is True
    assert meta["broad_spectrum"] is True
    assert meta["multi_side_plan"] is True
    assert meta["dropped_chunk_ids"] == ["software-1"]


def test_final_context_answerability_gate_skips_global_search_mode():
    sources = [
        _chunk(
            "global-1",
            doc_id="book-a.md",
            text="A global answer may need a source that carries a weak local term match.",
        ),
        _chunk(
            "global-2",
            doc_id="book-b.md",
            text="A second source broadens the corpus map.",
        ),
    ]

    gated, meta = chat_module._apply_final_context_answerability_gate(
        sources,
        query="What themes show up in this library?",
        search_mode="global",
        mode="soft",
        min_keep=1,
    )

    assert [chunk.chunk_id for chunk in gated] == ["global-1", "global-2"]
    assert meta["enabled"] is True
    assert meta["skipped"] is True
    assert meta["skip_reason"] == "global_search_mode"
    assert meta["dropped"] == 0


def test_compound_query_phrase_promotes_privacy_and_on_device_lanes():
    facets = chat_module._chat_coverage_facets_for_query(
        "How could privacy-preserving on-device AI help users reflect locally?"
    )
    by_name = {facet["name"]: facet for facet in facets}

    assert by_name["privacy"]["query_explicit"] is True
    assert by_name["on_device_llm"]["query_explicit"] is True
    assert by_name["privacy"]["source"] in {
        "compound_query_phrase",
        "query_deconstruction",
    }
    assert by_name["on_device_llm"]["source"] in {
        "compound_query_phrase",
        "query_deconstruction",
    }
    assert "privacy-preserving on-device ai" in [
        str(term).lower() for term in by_name["privacy"]["matched"]
    ]


def test_affect_as_ordinary_verb_does_not_trigger_neuro_narrative_lane():
    facets = chat_module._chat_coverage_facets_for_query(
        "What is NLP and how does Python affect it?"
    )

    assert "neuro_narrative" not in {facet["name"] for facet in facets}


@pytest.mark.asyncio
async def test_chat_semantic_coverage_forces_compound_privacy_on_device_lanes(monkeypatch):
    base_sources = [
        _chunk(
            "measurement-1",
            doc_id="measurement.md",
            text="Psychometric assessment can use scenario choices and values.",
        )
    ]

    async def fake_retrieve(**kwargs):
        query = kwargs["query"].lower()
        if "on-device" in query or "local llm" in query or "local inference" in query:
            chunks = [
                _chunk(
                    "on-device-1",
                    doc_id="on_device_llm_architecture_guide.md",
                    text=(
                        "On-device LLM architecture uses local inference and a "
                        "small language model so sensitive data stays on device."
                    ),
                    metadata={
                        "semantic_facets": {
                            "facet_ids": ["on_device_llm_architecture"],
                            "content_facet_ids": ["on_device_llm", "privacy"],
                            "content_facet_text": "on device llm privacy",
                        }
                    },
                )
            ]
        elif "privacy" in query or "data privacy" in query:
            chunks = [
                _chunk(
                    "privacy-1",
                    doc_id="privacy.md",
                    text=(
                        "Privacy-preserving systems keep private user data local, "
                        "minimize collection, and require consent."
                    ),
                    metadata={
                        "semantic_facets": {
                            "facet_ids": ["privacy"],
                            "content_facet_ids": ["privacy", "on_device_llm"],
                            "content_facet_text": "privacy on device llm",
                        }
                    },
                )
            ]
        else:
            chunks = []
        return RetrievalResult(
            chunks=chunks,
            requested_tier=kwargs["retrieval_tier"],
            effective_tier=kwargs["retrieval_tier"],
        )

    async def fake_facets(query, corpus_ids):
        return chat_module._chat_coverage_facets_for_query(query)

    monkeypatch.setattr(
        chat_module,
        "_chat_coverage_facets_for_query_with_corpus",
        fake_facets,
    )
    monkeypatch.setattr(chat_module.retriever_orchestrator, "retrieve", fake_retrieve)

    merged, meta = await chat_module._enforce_chat_query_coverage(
        original_query=(
            "How could privacy-preserving on-device AI support a personal "
            "reflection app without reducing someone to behavior data?"
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

    assert {"privacy", "on_device_llm"} <= set(meta["selected_facets"])
    assert {"privacy", "on_device_llm"} <= set(meta["explicit_missing_facets"])
    assert {"facet:privacy", "facet:on_device_llm"} <= set(meta["support_lanes"])
    assert {chunk.doc_id for chunk in merged} >= {
        "privacy.md",
        "on_device_llm_architecture_guide.md",
    }


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
    assert "Internal RAG evidence guardrail" in note
    assert "do not mention this block" in note
    assert "required these evidence areas" in note
    assert "Not source-backed in this retrieval packet: knowledge_graph" in note
    assert "Weakly source-backed areas: user_modeling" in note
    assert "HARD LIMIT" in note
    assert "no source-backed evidence" in note
    assert "Do not state these areas as existing capabilities" in note
    assert "not a generic pretrained definition" in note
    assert "Do not expose internal terms like facets, lanes" in note
    assert "Do not open with a corpus audit" in note


def test_system_prompt_includes_agent_zero_chat_rag_shape():
    prompt = chat_module._build_polymath_system_prompt()

    assert "Agent-Zero-inspired chat render style" in prompt
    assert "high-signal" in prompt
    assert "strongest one-sentence synthesis" in prompt
    assert "primary answer substrate" in prompt
    assert "instead of substituting your pretrained background knowledge" in prompt
    assert "Use tables first only when" in prompt
    assert "grid-style Markdown tables" in prompt
    assert "numbered lists" in prompt
    assert "fenced `json` block" in prompt
    assert "{ \"entities\": [...] }" in prompt
    assert "first substantial payload" in prompt
    assert "plain-text ASCII diagrams" in prompt
    assert "fenced `text` block" in prompt
    assert "tiny ASCII bar chart is allowed" in prompt
    assert "Mandatory display contract" in prompt
    assert "do not return a plain wall of prose" in prompt
    assert "graph, ontology, retrieval, data-flow" in prompt
    assert "Use bold anchors for scanability" in prompt
    assert "Reasoning bridges are welcome" in prompt
    assert "Use the `→` marker sparingly" in prompt
    assert "blockquotes only as brief margin annotations" in prompt
    assert "**Failure mode:**" in prompt
    assert "bold thesis, table or decision matrix" in prompt
    assert "short orientation paragraph" in prompt
    assert "not a graph-query report" in prompt
    assert "Never expose retrieval mechanics" in prompt
    assert "Do not use fixed Graph Query section labels" in prompt
    assert "content-driven headings" in prompt
    assert "natural RAG" in prompt
    assert "bold thesis → compact table or ASCII map" in prompt
    assert "pressure-tested synthesis" in prompt
    assert "what works, what is under-specified" in prompt
    assert "smallest credible prototype path" in prompt
    assert "Break ambitious concepts into sub-problems" in prompt
    assert "Use existing conversation context" in prompt
    assert "convergent validity" in prompt
    assert "`Orientation`" in prompt
    assert "`Direction`" in prompt
    assert "do not turn that into a retrieval-status section" in prompt


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


@pytest.mark.asyncio
async def test_chat_semantic_coverage_runs_facet_retrievals_concurrently(monkeypatch):
    """Per-facet coverage retrievals must run concurrently, not serially, so a
    multi-facet query does not pay N back-to-back retrieval passes. Proven by
    tracking the max number of in-flight retrievals."""
    import asyncio

    base_sources = [
        _chunk(
            "measurement-1",
            doc_id="measurement.md",
            text="Psychometric assessment can use scenario choices and values.",
        )
    ]

    state = {"in_flight": 0, "max_in_flight": 0}

    async def fake_retrieve(**kwargs):
        state["in_flight"] += 1
        state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        try:
            await asyncio.sleep(0.05)  # hold the slot so overlap is observable
            query = kwargs["query"].lower()
            if "on-device" in query or "local llm" in query or "local inference" in query:
                chunks = [
                    _chunk(
                        "on-device-1",
                        doc_id="on_device_llm_architecture_guide.md",
                        text=(
                            "On-device LLM architecture uses local inference and a "
                            "small language model so sensitive data stays on device."
                        ),
                        metadata={"semantic_facets": {
                            "facet_ids": ["on_device_llm_architecture"],
                            "content_facet_ids": ["on_device_llm", "privacy"],
                            "content_facet_text": "on device llm privacy"}},
                    )
                ]
            elif "privacy" in query or "data privacy" in query:
                chunks = [
                    _chunk(
                        "privacy-1",
                        doc_id="privacy.md",
                        text=(
                            "Privacy-preserving systems keep private user data local, "
                            "minimize collection, and require consent."
                        ),
                        metadata={"semantic_facets": {
                            "facet_ids": ["privacy"],
                            "content_facet_ids": ["privacy", "on_device_llm"],
                            "content_facet_text": "privacy on device llm"}},
                    )
                ]
            else:
                chunks = []
            return RetrievalResult(
                chunks=chunks,
                requested_tier=kwargs["retrieval_tier"],
                effective_tier=kwargs["retrieval_tier"],
            )
        finally:
            state["in_flight"] -= 1

    async def fake_facets(query, corpus_ids):
        return chat_module._chat_coverage_facets_for_query(query)

    monkeypatch.setattr(chat_module, "_chat_coverage_facets_for_query_with_corpus", fake_facets)
    monkeypatch.setattr(chat_module.retriever_orchestrator, "retrieve", fake_retrieve)

    merged, meta = await chat_module._enforce_chat_query_coverage(
        original_query=(
            "How could privacy-preserving on-device AI support a personal "
            "reflection app without reducing someone to behavior data?"
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

    # The two facet retrievals overlapped (serial would max out at 1).
    assert state["max_in_flight"] >= 2
    # And coverage quality is preserved: both facet docs still made the cut.
    assert {chunk.doc_id for chunk in merged} >= {
        "privacy.md",
        "on_device_llm_architecture_guide.md",
    }

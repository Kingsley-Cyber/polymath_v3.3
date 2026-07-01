from models.schemas import RetrievalTier, SourceChunk
from services.retriever.intent_policy import infer_retrieval_intent
from services.retriever import _trim_bounded_rerank_tail
from services.retriever.ranking_policy import (
    _required_atoms_for_query,
    apply_candidate_weights,
    apply_query_grounding,
    candidate_kind,
    select_with_diversity,
)


def _chunk(
    chunk_id: str,
    *,
    score: float,
    parent_id: str | None = None,
    doc_id: str | None = None,
    text: str | None = None,
    summary: str | None = None,
    source_tier: str = "tier_a",
    provenance: list[dict] | None = None,
    metadata: dict | None = None,
) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=parent_id or chunk_id,
        doc_id=doc_id or f"doc-{chunk_id}",
        corpus_id="corpus-1",
        text=text or summary or f"text {chunk_id}",
        summary=summary,
        score=score,
        source_tier=source_tier,
        provenance=provenance,
        metadata=metadata or {},
    )


def test_candidate_kind_detects_summary_lexical_and_child():
    assert candidate_kind(_chunk("c1", score=0.9)) == "child"
    assert candidate_kind(
        _chunk("p1_summary", score=0.9, summary="overview")
    ) == "summary"
    assert candidate_kind(
        _chunk("c2", score=0.9, source_tier="tier_a+lexical")
    ) == "lexical"


def test_broad_weighting_lifts_summary_over_near_child():
    intent = infer_retrieval_intent("summarize the main themes")
    chunks = [
        _chunk("child", score=0.90),
        _chunk("parent_summary", score=0.86, summary="theme overview"),
    ]

    weighted = apply_candidate_weights(
        chunks,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
    )

    assert weighted[0].chunk_id == "parent_summary"


def test_mmr_keeps_final_k_and_prefers_distinct_sources_for_broad_hybrid():
    intent = infer_retrieval_intent("summarize themes across documents")
    ranked = [
        _chunk("c1", score=1.00, parent_id="p1", doc_id="d1"),
        _chunk("c2", score=0.99, parent_id="p2", doc_id="d1"),
        _chunk("c3", score=0.97, parent_id="p3", doc_id="d2"),
        _chunk("c4", score=0.95, parent_id="p4", doc_id="d3"),
    ]

    result = select_with_diversity(
        ranked,
        final_top_k=3,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
    )

    assert len(result.candidates) == 3
    assert result.added == 1
    assert [c.chunk_id for c in result.candidates] == ["c1", "c3", "c4"]


def test_diversity_skips_weak_or_duplicate_candidates():
    intent = infer_retrieval_intent("summarize themes across documents")
    ranked = [
        _chunk("c1", score=1.00, parent_id="p1", doc_id="d1"),
        _chunk("c2", score=0.96, parent_id="p2", doc_id="d2"),
        _chunk("dup", score=0.95, parent_id="p1", doc_id="d1"),
        _chunk("weak", score=0.20, parent_id="p3", doc_id="d3"),
    ]

    result = select_with_diversity(
        ranked,
        final_top_k=2,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
    )

    assert result.added == 0
    assert [c.chunk_id for c in result.candidates] == ["c1", "c2"]


def test_diversity_floor_rejects_low_relevance_different_doc():
    intent = infer_retrieval_intent("what is NLP")
    ranked = [
        _chunk(
            "definition",
            score=1.00,
            doc_id="d1",
            text="NLP is natural language processing.",
            metadata={"query_grounding": {"matched": ["nlp"]}},
        ),
        _chunk(
            "same-doc-detail",
            score=0.98,
            doc_id="d1",
            text="NLP systems process human language and text.",
            metadata={"query_grounding": {"matched": ["nlp"]}},
        ),
        _chunk(
            "weak-other-doc",
            score=0.50,
            doc_id="d2",
            text="A language mention appears in a loosely related note.",
        ),
    ]

    result = select_with_diversity(
        ranked,
        final_top_k=2,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
        query="what is NLP",
    )

    assert [c.chunk_id for c in result.candidates] == ["definition", "same-doc-detail"]
    assert "weak-other-doc" not in [c.chunk_id for c in result.candidates]


def test_sufficiency_repair_replaces_diverse_chunk_with_missing_atom():
    intent = infer_retrieval_intent(
        "what is nlp and how does it associate with data augmentation"
    )
    ranked = [
        _chunk(
            "definition",
            score=1.00,
            doc_id="d1",
            text="NLP is natural language processing for human language.",
            metadata={"query_grounding": {"matched": ["nlp"]}},
        ),
        _chunk(
            "data-only",
            score=0.99,
            doc_id="d2",
            text="Data augmentation creates additional training data.",
            metadata={"query_grounding": {"matched": ["data", "augmentation"]}},
        ),
        _chunk(
            "relationship",
            score=0.86,
            doc_id="d1",
            text=(
                "NLP is associated with data augmentation when augmented text "
                "examples improve language model training."
            ),
            metadata={
                "query_grounding": {"matched": ["nlp", "data", "augmentation"]}
            },
        ),
    ]

    result = select_with_diversity(
        ranked,
        final_top_k=2,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
        query="what is nlp and how does it associate with data augmentation",
    )

    ids = [c.chunk_id for c in result.candidates]
    assert ids == ["definition", "relationship"]
    repaired = result.candidates[1].metadata["diversity_rerank"]
    assert repaired["selected_by"] == "sufficiency_repair"
    assert result.diagnostics["sufficiency"]["answerable"] is True
    assert result.diagnostics["repair_rounds"] == 1


def test_diversity_includes_high_confidence_document_anchor_candidate():
    intent = infer_retrieval_intent("compare evidence from two named books")
    ranked = [
        _chunk("c1", score=0.80, parent_id="p1", doc_id="d1"),
        _chunk("c2", score=-1.00, parent_id="p2", doc_id="d2"),
        _chunk(
            "anchor",
            score=-6.00,
            parent_id="p3",
            doc_id="d3",
            source_tier="document_anchor+lexical",
            provenance=[
                {
                    "retriever": "document_anchor",
                    "document_score": 0.98,
                }
            ],
        ),
    ]

    result = select_with_diversity(
        ranked,
        final_top_k=2,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
    )

    assert result.added == 1
    assert [c.chunk_id for c in result.candidates] == ["c1", "anchor"]


def test_fast_search_uses_mmr_without_expanding_final_sources():
    intent = infer_retrieval_intent("summarize themes across documents")
    ranked = [
        _chunk("c1", score=1.00, parent_id="p1", doc_id="d1"),
        _chunk("c2", score=0.99, parent_id="p2", doc_id="d1"),
        _chunk("c3", score=0.97, parent_id="p3", doc_id="d2"),
    ]

    result = select_with_diversity(
        ranked,
        final_top_k=2,
        intent=intent,
        tier=RetrievalTier.qdrant_only,
    )

    assert result.added == 1
    assert [c.chunk_id for c in result.candidates] == ["c1", "c3"]


def test_probability_rerank_tail_trim_drops_near_zero_fillers():
    ranked = [
        _chunk("strong", score=0.94),
        _chunk("good", score=0.64),
        _chunk("junk", score=0.004),
    ]

    trimmed = _trim_bounded_rerank_tail(
        ranked,
        rerank_enabled=True,
        score_scale="probability",
        tier=RetrievalTier.qdrant_only,
    )

    assert [c.chunk_id for c in trimmed] == ["strong", "good"]


def test_probability_rerank_tail_trim_keeps_low_confidence_pool():
    ranked = [
        _chunk("weak-best", score=0.22),
        _chunk("weak-next", score=0.03),
    ]

    trimmed = _trim_bounded_rerank_tail(
        ranked,
        rerank_enabled=True,
        score_scale="probability",
        tier=RetrievalTier.qdrant_only,
    )

    assert trimmed == ranked


def test_probability_rerank_tail_trim_skips_mixed_scale_pool():
    ranked = [
        _chunk("raw-code-score", score=103.0),
        _chunk("good-prose", score=0.72),
        _chunk("good-prose-2", score=0.64),
    ]

    trimmed = _trim_bounded_rerank_tail(
        ranked,
        rerank_enabled=True,
        score_scale="probability",
        tier=RetrievalTier.qdrant_only,
    )

    assert trimmed == ranked


def test_probability_rerank_tail_trim_disabled_for_hydrated_tiers():
    ranked = [
        _chunk("strong", score=0.94),
        _chunk("good", score=0.64),
        _chunk("low-but-possibly-useful", score=0.004),
    ]

    hybrid = _trim_bounded_rerank_tail(
        ranked,
        rerank_enabled=True,
        score_scale="probability",
        tier=RetrievalTier.qdrant_mongo,
    )
    graph = _trim_bounded_rerank_tail(
        ranked,
        rerank_enabled=True,
        score_scale="probability",
        tier=RetrievalTier.qdrant_mongo_graph,
    )

    assert hybrid == ranked
    assert graph == ranked


def test_logit_rerank_tail_trim_is_disabled():
    ranked = [
        _chunk("strong", score=4.0),
        _chunk("negative", score=-3.0),
    ]

    trimmed = _trim_bounded_rerank_tail(
        ranked,
        rerank_enabled=True,
        score_scale="logit",
    )

    assert trimmed == ranked


def test_query_grounding_promotes_complete_concept_coverage():
    ranked = [
        _chunk(
            "python-only",
            score=1.0,
            text="Python supports threads, locks, descriptors, and protocols.",
        ),
        _chunk(
            "nlp-python",
            score=0.62,
            text=(
                "Natural language processing systems are often prototyped in "
                "Python with libraries for tokenization and modeling."
            ),
        ),
        _chunk(
            "sql-only",
            score=1.0,
            text="The relational assignment grammar updates a relvar.",
        ),
    ]

    grounded = apply_query_grounding(
        ranked,
        query="what is nlp and its relation to python",
        tier=RetrievalTier.qdrant_mongo_graph,
        score_scale="probability",
    )

    assert grounded[0].chunk_id == "nlp-python"
    assert grounded[-1].chunk_id == "sql-only"
    assert grounded[0].metadata["query_grounding"]["matched"] == ["nlp", "python"]


def test_query_grounding_expands_nlp_acronym_alias():
    ranked = [
        _chunk(
            "expanded",
            score=0.4,
            text="Natural language processing studies computational language.",
        ),
        _chunk(
            "unrelated",
            score=1.0,
            text="Python decorators wrap functions.",
        ),
    ]

    grounded = apply_query_grounding(
        ranked,
        query="define NLP",
        tier=RetrievalTier.qdrant_mongo,
        score_scale="probability",
    )

    assert grounded[0].chunk_id == "expanded"
    assert grounded[0].metadata["query_grounding"]["matched"] == ["nlp"]


def test_correlation_is_relationship_operator_not_content_concept():
    required = _required_atoms_for_query(
        "how does personality correlate with seduction"
    )

    assert "relationship" in required
    assert "concept:correlate" not in required
    assert "concept:correlation" not in required


def test_query_grounding_ignores_correlation_as_standalone_concept():
    ranked = [
        _chunk(
            "stats-only",
            score=1.0,
            text="A model correlation differs across slices of data.",
        ),
        _chunk(
            "semantic-match",
            score=0.70,
            text=(
                "Personality profiles shape how seductive character and "
                "seduction tactics are interpreted."
            ),
        ),
    ]

    grounded = apply_query_grounding(
        ranked,
        query=(
            "different personality correlation seduction with people as men "
            "dating women"
        ),
        tier=RetrievalTier.qdrant_mongo,
        score_scale="probability",
    )

    assert grounded[0].chunk_id == "semantic-match"
    assert grounded[0].metadata["query_grounding"]["matched"] == [
        "personality",
        "seduction",
    ]


def test_query_grounding_maps_personality_to_character_types():
    ranked = [
        _chunk(
            "victim-types",
            score=0.50,
            text=(
                "The seducer studies victim types, character traits, and "
                "seductive behavior."
            ),
        ),
        _chunk(
            "literal-only",
            score=0.80,
            text="A personality inventory lists neutral questionnaire scales.",
        ),
    ]

    grounded = apply_query_grounding(
        ranked,
        query="personality seduction",
        tier=RetrievalTier.qdrant_mongo,
        score_scale="probability",
    )

    assert grounded[0].chunk_id == "victim-types"
    assert grounded[0].metadata["query_grounding"]["matched"] == [
        "personality",
        "seduction",
    ]


def test_personality_framework_relationship_requires_second_source():
    intent = infer_retrieval_intent(
        "how does different personality correlate to the art of seduction"
    )
    ranked = [
        _chunk(
            "seduction-core",
            score=1.00,
            doc_id="seduction-book",
            text=(
                "Seduction relates to seductive character and the way a "
                "person excites desire."
            ),
            metadata={"query_grounding": {"matched": ["personality", "seduction"]}},
        ),
        _chunk(
            "seduction-detail",
            score=0.98,
            doc_id="seduction-book",
            text="The seducer studies character traits and seductive behavior.",
            metadata={"query_grounding": {"matched": ["personality", "seduction"]}},
        ),
        _chunk(
            "personality-framework",
            score=0.70,
            doc_id="personality-book",
            text=(
                "The Four Tendencies is a personality framework for explaining "
                "how different personality tendencies respond to expectations."
            ),
            metadata={
                "query_grounding": {
                    "matched": ["personality_framework", "personality"]
                }
            },
        ),
    ]

    result = select_with_diversity(
        ranked,
        final_top_k=3,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
        query="how does different personality correlate to the art of seduction",
    )

    ids = [c.chunk_id for c in result.candidates]
    assert "personality-framework" in ids
    assert {c.doc_id for c in result.candidates} == {
        "seduction-book",
        "personality-book",
    }
    assert result.diagnostics["sufficiency"]["answerable"] is True
    assert result.diagnostics["sufficiency"]["relationship_distinct_docs"] == 2


def test_graph_tier_reserves_slot_for_demoted_graph_expansion():
    """A graph-expanded neighbor demoted by the cross-encoder still reaches the
    LLM via the graph-provenance reservation in select_with_diversity."""
    intent = infer_retrieval_intent("what is layering")
    ranked = [
        _chunk(f"core-{i}", score=0.90 - i * 0.01, doc_id=f"d{i}") for i in range(6)
    ]
    # Relational neighbor scored low by text-similarity rerank, distinct doc/parent.
    ranked.append(
        _chunk("graph-neighbor", score=0.40, source_tier="graph_mode_a", doc_id="gdoc")
    )
    result = select_with_diversity(
        ranked,
        final_top_k=5,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo_graph,
    )
    ids = [c.chunk_id for c in result.candidates]
    assert "graph-neighbor" in ids  # reserved despite low rerank score


def test_hybrid_tier_does_not_reserve_graph_slot():
    """Reservation is graph-tier only; Hybrid leaves a low-score chunk excluded."""
    intent = infer_retrieval_intent("what is layering")
    ranked = [
        _chunk(f"core-{i}", score=0.90 - i * 0.01, doc_id=f"d{i}") for i in range(6)
    ]
    ranked.append(
        _chunk("graph-neighbor", score=0.40, source_tier="graph_mode_a", doc_id="gdoc")
    )
    result = select_with_diversity(
        ranked,
        final_top_k=5,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
    )
    ids = [c.chunk_id for c in result.candidates]
    assert "graph-neighbor" not in ids


def test_specific_intent_allows_target_doc_beyond_two_seats():
    # Live-probe regression (2026-07-01): SPECIFIC hard_doc_cap=2 forced 6 of
    # 8 final seats to off-topic docs when the query targeted one book.
    # The target doc's 3rd-best chunk must be able to out-compete weak
    # foreign chunks (it pays the soft-cap penalty but is not forbidden).
    intent = infer_retrieval_intent(
        "According to Eric Berne, what is a game in human relationships "
        "and what are its key elements?"
    )
    assert str(intent.need) == "QueryNeed.SPECIFIC"
    ranked = [
        _chunk("b1", score=0.98, parent_id="p1", doc_id="berne",
               text="games are patterned transactions with a concealed payoff"),
        _chunk("b2", score=0.97, parent_id="p2", doc_id="berne",
               text="every game has a gimmick a switch and a payoff element"),
        _chunk("b3", score=0.96, parent_id="p3", doc_id="berne",
               text="ulterior transactions distinguish games from pastimes and rituals"),
        _chunk("j1", score=0.55, parent_id="p4", doc_id="flutter",
               text="widget trees rebuild when state changes in the framework"),
        _chunk("j2", score=0.54, parent_id="p5", doc_id="refactoring-ui",
               text="visual hierarchy is established with font size and color"),
        _chunk("j3", score=0.53, parent_id="p6", doc_id="c-stdlib",
               text="the allocator returns aligned storage for object lifetimes"),
    ]

    result = select_with_diversity(
        ranked,
        final_top_k=5,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
    )

    berne_seats = sum(1 for c in result.candidates if c.doc_id == "berne")
    assert berne_seats >= 3


def test_specific_intent_trims_weak_diversity_fillers():
    # Live-probe regression (2026-07-01): tangential chunks scoring in the
    # 0.25-0.5 band ("Flutter for Jobseekers" in an Eric Berne query) were
    # seated by the relaxed door / novelty bonuses. For SPECIFIC intent the
    # post-MMR trim holds seats to a 0.5-of-top floor — fewer, on-topic
    # chunks beat a padded final set.
    intent = infer_retrieval_intent(
        "According to Eric Berne, what is a game in human relationships "
        "and what are its key elements?"
    )
    ranked = [
        _chunk("b1", score=0.98, parent_id="p1", doc_id="berne",
               text="games are patterned transactions with a concealed payoff"),
        _chunk("b2", score=0.95, parent_id="p2", doc_id="berne",
               text="every game has a gimmick a switch and a payoff element"),
        _chunk("j1", score=0.30, parent_id="p3", doc_id="flutter",
               text="widget trees rebuild when state changes in the framework"),
        _chunk("j2", score=0.28, parent_id="p4", doc_id="refactoring-ui",
               text="visual hierarchy is established with font size and color"),
    ]

    result = select_with_diversity(
        ranked,
        final_top_k=4,
        intent=intent,
        tier=RetrievalTier.qdrant_mongo,
    )

    final_docs = {c.doc_id for c in result.candidates}
    assert final_docs == {"berne"}, f"weak fillers must be trimmed, got {final_docs}"
    assert result.diagnostics.get("specific_floor_trimmed", 0) >= 1

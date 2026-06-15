from models.schemas import RetrievalTier, SourceChunk, SourceFact
from services.retriever import (
    _fact_seed_chunks,
    _has_query_term_overlap,
    _lexical_limit_for,
    _retrieval_store_contract,
    _should_drop_low_confidence_rerank,
)
from services.retriever.lexical import _regex_score, _terms


def test_speed_profiles_map_to_lexical_budget():
    assert _lexical_limit_for(
        RetrievalTier.qdrant_only,
        retrieval_k=60,
        rerank_enabled=True,
    ) == 0
    assert _lexical_limit_for(
        RetrievalTier.qdrant_mongo,
        retrieval_k=10,
        rerank_enabled=False,
    ) == 6
    assert _lexical_limit_for(
        RetrievalTier.qdrant_mongo,
        retrieval_k=40,
        rerank_enabled=True,
    ) == 12
    assert _lexical_limit_for(
        RetrievalTier.qdrant_mongo_graph,
        retrieval_k=60,
        rerank_enabled=True,
    ) == 18


def test_retrieval_store_contracts_make_tiers_observable():
    vector = _retrieval_store_contract(RetrievalTier.qdrant_only)
    assert vector["label"] == "Vector Base"
    assert vector["qdrant_vectors"] is True
    assert vector["qdrant_summaries"] is True
    assert vector["mongo_lexical"] is False
    assert vector["neo4j_facts"] is False
    assert vector["neo4j_expansion"] is False

    hybrid = _retrieval_store_contract(RetrievalTier.qdrant_mongo)
    assert hybrid["label"] == "Hybrid"
    assert hybrid["qdrant_vectors"] is True
    assert hybrid["mongo_lexical"] is True
    assert hybrid["mongo_hydration"] is True
    assert hybrid["neo4j_facts"] is False

    graph = _retrieval_store_contract(RetrievalTier.qdrant_mongo_graph)
    assert graph["label"] == "Graph Augmented"
    assert graph["qdrant_vectors"] is True
    assert graph["mongo_lexical"] is True
    assert graph["neo4j_facts"] is True
    assert graph["neo4j_expansion"] is True


def test_lexical_terms_drop_stop_words_and_duplicates():
    assert _terms("How does TensorFlow Lite use TensorFlow on-device?") == [
        "tensorflow",
        "lite",
        "use",
        "on-device",
    ]


def test_regex_score_rewards_exact_heading_matches():
    query = "Architecture Feasibility Report"
    terms = _terms(query)
    row = {
        "heading_path": ["Architecture_Feasibility_Report"],
        "text": "This section evaluates implementation constraints.",
    }
    assert _regex_score(query, terms, row) > 0.7


def test_low_confidence_guard_drops_unrelated_rerank_results():
    ranked = [
        SourceChunk(
            chunk_id="c1",
            parent_id="p1",
            doc_id="d1",
            corpus_id="corpus",
            text="weighted regression and out-of-the-money option glossary",
            score=-2.841,
            source_tier="chunk",
        )
    ]
    assert _should_drop_low_confidence_rerank(
        ranked,
        "what is chldani",
        rerank_enabled=True,
        score_scale="logit",
        low_confidence_threshold=-2.5,
    )


def test_low_confidence_guard_keeps_exact_term_overlap():
    ranked = [
        SourceChunk(
            chunk_id="c1",
            parent_id="p1",
            doc_id="d1",
            corpus_id="corpus",
            text="Chladni patterns are standing-wave figures produced by vibration.",
            score=-2.841,
            source_tier="chunk",
        )
    ]
    assert _has_query_term_overlap(ranked, "what is Chladni")
    assert not _should_drop_low_confidence_rerank(
        ranked,
        "what is Chladni",
        rerank_enabled=True,
        score_scale="logit",
        low_confidence_threshold=-2.5,
    )


def test_low_confidence_guard_ignores_bounded_score_scales():
    ranked = [
        SourceChunk(
            chunk_id="c1",
            parent_id="p1",
            doc_id="d1",
            corpus_id="corpus",
            text="weighted regression and out-of-the-money option glossary",
            score=0.01,
            source_tier="chunk",
        )
    ]
    assert not _should_drop_low_confidence_rerank(
        ranked,
        "what is chldani",
        rerank_enabled=True,
        score_scale="cosine",
        low_confidence_threshold=-2.5,
    )


def test_fact_seed_chunks_point_back_to_supporting_chunks():
    facts = [
        SourceFact(
            fact_id="f1",
            subject="Graph Augmented",
            fact_type="property",
            property_name="retrieval_order",
            value="fact-first",
            confidence=0.9,
            evidence_phrase="Graph Augmented starts from facts.",
            chunk_id="chunk-1",
            doc_id="doc-1",
            corpus_id="corpus-1",
        ),
        SourceFact(
            fact_id="f2",
            subject="Graph Augmented",
            fact_type="property",
            property_name="duplicate",
            value="same chunk",
            confidence=0.8,
            chunk_id="chunk-1",
            doc_id="doc-1",
            corpus_id="corpus-1",
        ),
        SourceFact(
            fact_id="f3",
            subject="No source",
            fact_type="property",
            property_name="ignored",
            value="missing chunk",
            confidence=1.0,
        ),
    ]

    chunks = _fact_seed_chunks(facts)

    assert len(chunks) == 1
    assert chunks[0].chunk_id == "chunk-1"
    assert chunks[0].parent_id == ""
    assert chunks[0].source_tier == "graph_fact_seed"
    assert chunks[0].score > 0.9
    assert chunks[0].provenance[0]["retriever"] == "neo4j_fact"
    assert "fact-first" in chunks[0].text

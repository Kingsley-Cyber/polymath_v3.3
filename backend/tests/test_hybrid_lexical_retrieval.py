from models.schemas import RetrievalTier, SourceChunk
from services.retriever import (
    _has_query_term_overlap,
    _lexical_limit_for,
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
    ) == 0
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
    )


def test_low_confidence_guard_skips_cosine_reranker_scores():
    ranked = [
        SourceChunk(
            chunk_id="c1",
            parent_id="p1",
            doc_id="d1",
            corpus_id="corpus",
            text="weighted regression and out-of-the-money option glossary",
            score=0.03,
            source_tier="chunk",
        )
    ]
    assert not _should_drop_low_confidence_rerank(
        ranked,
        "what is chldani",
        rerank_enabled=True,
        score_scale="cosine",
    )

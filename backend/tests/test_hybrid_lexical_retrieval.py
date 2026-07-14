import pytest

from models.schemas import RetrievalTier, SourceChunk, SourceFact
from qdrant_client import models as qmodels
from services.retriever import (
    _fact_seed_chunks,
    _has_query_term_overlap,
    _lexical_limit_for,
    _retrieval_store_contract,
    _should_drop_low_confidence_rerank,
)
from services.retriever.funnel_a import FunnelA
from services.retriever.funnel_b import FunnelB
from services.retriever.lexical import LexicalRetriever, _regex_score, _terms
from services.storage import qdrant_writer


class _FakeQdrantHit:
    id = "point-1"
    score = 0.77
    payload = {
        "chunk_id": "chunk-1",
        "parent_id": "parent-1",
        "doc_id": "doc-1",
        "corpus_id": "corpus-1",
        "chunk_text": "NSN 5340-01-234-5678 appears in this logistics note.",
        "source_tier": "tier_a",
        "chunk_kind": "body",
    }


class _FakeQdrantClient:
    def __init__(self, *, fail_first: bool = False):
        self.calls: list[dict] = []
        self.fail_first = fail_first

    async def query_points(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_first and len(self.calls) == 1:
            raise RuntimeError("rrf unavailable")
        return type("Resp", (), {"points": [_FakeQdrantHit()]})()


def test_speed_profiles_map_to_lexical_budget():
    assert (
        _lexical_limit_for(
            RetrievalTier.qdrant_only,
            retrieval_k=60,
            rerank_enabled=True,
        )
        == 0
    )
    assert (
        _lexical_limit_for(
            RetrievalTier.qdrant_mongo,
            retrieval_k=10,
            rerank_enabled=False,
        )
        == 6
    )
    assert (
        _lexical_limit_for(
            RetrievalTier.qdrant_mongo,
            retrieval_k=40,
            rerank_enabled=True,
        )
        == 12
    )
    assert (
        _lexical_limit_for(
            RetrievalTier.qdrant_mongo_graph,
            retrieval_k=60,
            rerank_enabled=True,
        )
        == 18
    )


def test_retrieval_store_contracts_make_tiers_observable():
    vector = _retrieval_store_contract(RetrievalTier.qdrant_only)
    assert vector["label"] == "Fast Search"
    assert vector["qdrant_vectors"] is True
    assert vector["qdrant_sparse"] is True
    assert vector["qdrant_rrf"] is True
    assert vector["qdrant_summaries"] is True
    assert vector["mongo_lexical"] is False
    assert vector["neo4j_facts"] is False
    assert vector["neo4j_expansion"] is False

    hybrid = _retrieval_store_contract(RetrievalTier.qdrant_mongo)
    assert hybrid["label"] == "Hybrid Search"
    assert hybrid["qdrant_vectors"] is True
    assert hybrid["qdrant_sparse"] is True
    assert hybrid["mongo_lexical"] is True
    assert hybrid["mongo_hydration"] is True
    assert hybrid["neo4j_facts"] is False

    graph = _retrieval_store_contract(RetrievalTier.qdrant_mongo_graph)
    assert graph["label"] == "Graph Augmentation"
    assert graph["qdrant_vectors"] is True
    assert graph["qdrant_sparse"] is True
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


async def _fake_collection_layout(_client, _collection_name):
    return True, True


async def _fake_legacy_collection_layout(_client, _collection_name):
    return True, False


@pytest.mark.asyncio
async def test_fast_funnel_b_uses_qdrant_dense_sparse_rrf_when_available(monkeypatch):
    monkeypatch.setattr(qdrant_writer, "_collection_layout", _fake_collection_layout)
    client = _FakeQdrantClient()
    funnel = FunnelB()
    funnel.client = client

    chunks = await funnel._search_collection(
        "corpus_abcd_naive",
        [0.1, 0.2],
        qmodels.Filter(must=[]),
        5,
        query_text="NSN 5340-01-234-5678",
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert isinstance(call["query"], qmodels.FusionQuery)
    assert call["query"].fusion == qmodels.Fusion.RRF
    assert {prefetch.using for prefetch in call["prefetch"]} == {"dense", "sparse"}
    dense_prefetch = next(p for p in call["prefetch"] if p.using == "dense")
    assert dense_prefetch.params.quantization.rescore is True
    assert dense_prefetch.params.quantization.oversampling == 2.0
    assert "query_filter" not in call
    assert chunks[0].chunk_id == "chunk-1"
    retrievers = {item["retriever"] for item in chunks[0].provenance}
    assert {"qdrant_dense", "qdrant_sparse", "qdrant_rrf"} <= retrievers


@pytest.mark.asyncio
async def test_fast_funnel_a_summaries_use_qdrant_dense_sparse_rrf_when_available(
    monkeypatch,
):
    monkeypatch.setattr(qdrant_writer, "_collection_layout", _fake_collection_layout)
    client = _FakeQdrantClient()
    funnel = FunnelA()
    funnel.client = client

    chunks = await funnel._search_collection(
        "corpus_abcd_hrag",
        [0.1, 0.2],
        qmodels.Filter(must=[]),
        5,
        query_text="para 3-2.1",
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert isinstance(call["query"], qmodels.FusionQuery)
    assert call["query"].fusion == qmodels.Fusion.RRF
    assert {prefetch.using for prefetch in call["prefetch"]} == {"dense", "sparse"}
    retrievers = {item["retriever"] for item in chunks[0].provenance}
    assert {"qdrant_dense_summary", "qdrant_sparse_summary", "qdrant_rrf"} <= retrievers


@pytest.mark.asyncio
async def test_fast_funnel_b_falls_back_to_dense_for_legacy_collection(monkeypatch):
    monkeypatch.setattr(
        qdrant_writer, "_collection_layout", _fake_legacy_collection_layout
    )
    client = _FakeQdrantClient()
    funnel = FunnelB()
    funnel.client = client

    chunks = await funnel._search_collection(
        "corpus_legacy_naive",
        [0.1, 0.2],
        qmodels.Filter(must=[]),
        5,
        query_text="NSN 5340-01-234-5678",
    )

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["query"] == [0.1, 0.2]
    assert call["using"] == "dense"
    assert call["search_params"].quantization.rescore is True
    assert call["search_params"].quantization.oversampling == 2.0
    assert "prefetch" not in call
    assert chunks[0].provenance == [{"retriever": "qdrant_dense"}]


@pytest.mark.asyncio
async def test_fast_funnel_b_rrf_failure_falls_back_to_dense(monkeypatch):
    monkeypatch.setattr(qdrant_writer, "_collection_layout", _fake_collection_layout)
    client = _FakeQdrantClient(fail_first=True)
    funnel = FunnelB()
    funnel.client = client

    chunks = await funnel._search_collection(
        "corpus_abcd_naive",
        [0.1, 0.2],
        qmodels.Filter(must=[]),
        5,
        query_text="NSN 5340-01-234-5678",
    )

    assert len(client.calls) == 2
    assert isinstance(client.calls[0]["query"], qmodels.FusionQuery)
    assert client.calls[1]["query"] == [0.1, 0.2]
    assert client.calls[1]["using"] == "dense"
    assert client.calls[1]["search_params"].quantization.rescore is True
    assert chunks[0].provenance == [{"retriever": "qdrant_dense"}]


@pytest.mark.asyncio
async def test_funnel_b_filters_children_by_document_and_parent(monkeypatch):
    monkeypatch.setattr(qdrant_writer, "_collection_layout", _fake_collection_layout)
    client = _FakeQdrantClient()
    funnel = FunnelB()
    funnel.client = client

    await funnel.search(
        [0.1, 0.2],
        ["corpus-1"],
        ["corpus_abcd_naive"],
        query_text="book recommendations",
        doc_ids=["doc-1"],
        parent_ids=["parent-1"],
    )

    conditions = client.calls[0]["prefetch"][0].filter.must
    fields = {condition.key: condition.match for condition in conditions}
    assert fields["doc_id"].any == ["doc-1"]
    assert fields["parent_id"].any == ["parent-1"]


@pytest.mark.asyncio
async def test_lexical_sparse_search_honors_routed_document_scope(monkeypatch):
    client = _FakeQdrantClient()
    retriever = LexicalRetriever()
    retriever._qdrant = client

    await retriever._qdrant_sparse_search(
        "book recommendations",
        ["corpus-1"],
        top_k=5,
        doc_ids=["doc-1"],
    )

    conditions = client.calls[0]["query_filter"].must
    fields = {condition.key: condition.match for condition in conditions}
    assert fields["doc_id"].any == ["doc-1"]


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
            subject="Graph Augmentation",
            fact_type="property",
            property_name="retrieval_order",
            value="fact-first",
            confidence=0.9,
            evidence_phrase="Graph Augmentation starts from facts.",
            chunk_id="chunk-1",
            doc_id="doc-1",
            corpus_id="corpus-1",
        ),
        SourceFact(
            fact_id="f2",
            subject="Graph Augmentation",
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

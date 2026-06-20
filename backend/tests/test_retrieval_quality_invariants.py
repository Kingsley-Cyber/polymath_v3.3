"""Portable retrieval-quality invariants — runs on ANY device, NO live stack.

These are pure-logic unit tests (synthetic data, no Mongo/Qdrant/Neo4j/LLM, no
seeded corpus) that pin the four retrieval-quality fixes so they cannot
silently regress on a fresh clone:

  Phase 1  candidate-collapse: per-doc cap + relative noise floor + MIN_KEEP,
           graph-provenance exempt (services/retriever/ranking_policy.py)
  Phase 2  fusion normalization: sparse lane scaled to [0,1]
           (services/retriever/lexical.py:_normalize_scores_to_unit)
  Phase 3  GERG edge query-ranking: subject-match required, definitional bonus
           only on top, off-topic dropped (services/retriever/graph_decoration.py)

The live, data-dependent e2e harnesses (test_*_e2e.py) prove the same behaviors
end-to-end against the running stack; THIS file is what guarantees the logic is
correct on a machine that has only the code. Run anywhere: `pytest
tests/test_retrieval_quality_invariants.py -q` (no services, no env).
"""
import types
from collections import Counter

from models.schemas import RetrievalTier, SourceChunk
from services.retriever.intent_policy import QueryNeed, infer_retrieval_intent
from services.retriever.ranking_policy import _per_doc_cap_for, select_with_diversity
from services.retriever.lexical import _normalize_scores_to_unit
from services.retriever.graph_decoration import _edge_query_relevance, _query_rank_rows
from services.retriever.query_grounding import concept_groups


def _chunk(chunk_id, *, score, doc_id=None, parent_id=None, source_tier="tier_a"):
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=parent_id or chunk_id,
        doc_id=doc_id or f"doc-{chunk_id}",
        corpus_id="c1",
        text=f"text {chunk_id}",
        score=score,
        source_tier=source_tier,
    )


# ── Phase 1 — candidate-collapse hygiene ──────────────────────────────────

def test_per_doc_cap_for_is_intent_adaptive():
    cases = {QueryNeed.SPECIFIC: 2, QueryNeed.BALANCED: 2, QueryNeed.BROAD: 1}
    for need, expected in cases.items():
        assert _per_doc_cap_for(types.SimpleNamespace(need=need), 8) == expected
    # ceil division, floored at 1
    assert _per_doc_cap_for(types.SimpleNamespace(need=QueryNeed.SPECIFIC), 5) == 2
    assert _per_doc_cap_for(types.SimpleNamespace(need=QueryNeed.BROAD), 3) == 1


def test_per_doc_cap_breaks_single_doc_domination():
    intent = infer_retrieval_intent("what is X and how does it relate to Y")
    cap = _per_doc_cap_for(intent, 8)
    ranked = [_chunk(f"d{i}", score=0.95 - i * 0.01, parent_id=f"pD{i}", doc_id="DOM") for i in range(6)]
    ranked += [_chunk(f"o{j}", score=0.62 - j * 0.01, parent_id=f"pO{j}", doc_id=f"O{j}") for j in range(4)]
    res = select_with_diversity(ranked, final_top_k=8, intent=intent, tier=RetrievalTier.qdrant_mongo)
    counts = Counter(c.doc_id for c in res.candidates)
    assert counts["DOM"] <= cap, counts            # the 6/9-from-one-book pathology
    assert max(counts.values()) <= cap


def test_relative_floor_drops_subfloor_nongraph_chunk():
    intent = infer_retrieval_intent("define the term clearly")
    ranked = [
        _chunk("a", score=0.90, doc_id="d1"),
        _chunk("b", score=0.80, doc_id="d2"),
        _chunk("c", score=0.70, doc_id="d3"),
        _chunk("junk", score=0.05, doc_id="d4"),   # below floor max(0.10, 0.25*0.90)=0.225
    ]
    res = select_with_diversity(ranked, final_top_k=8, intent=intent, tier=RetrievalTier.qdrant_mongo)
    assert "junk" not in [c.chunk_id for c in res.candidates]


def test_graph_chunk_exempt_from_floor():
    intent = infer_retrieval_intent("how does X work")
    ranked = [_chunk(f"c{i}", score=0.9 - i * 0.05, doc_id=f"d{i}") for i in range(5)]
    ranked.append(_chunk("g", score=0.05, source_tier="graph_mode_a", doc_id="gd"))  # below floor
    res = select_with_diversity(ranked, final_top_k=5, intent=intent, tier=RetrievalTier.qdrant_mongo_graph)
    assert "g" in [c.chunk_id for c in res.candidates]   # reserved despite below floor


def test_min_keep_never_strands_pool():
    intent = infer_retrieval_intent("define the term")
    ranked = [
        _chunk("top", score=0.90, doc_id="d1"),
        _chunk("low1", score=0.05, doc_id="d2"),
        _chunk("low2", score=0.04, doc_id="d3"),
    ]
    res = select_with_diversity(ranked, final_top_k=8, intent=intent, tier=RetrievalTier.qdrant_mongo)
    assert len(res.candidates) >= min(8, 3)   # floor would leave 1; MIN_KEEP backfills to >=3


def test_vector_base_unchanged_strict_topk():
    intent = infer_retrieval_intent("summarize the themes")
    ranked = [_chunk(f"c{i}", score=0.9 - i * 0.1, doc_id="SAME") for i in range(4)]
    res = select_with_diversity(ranked, final_top_k=2, intent=intent, tier=RetrievalTier.qdrant_only)
    # Vector base is strict top-k: no per-doc cap, no floor, no extras.
    assert [c.chunk_id for c in res.candidates] == ["c0", "c1"]
    assert res.added == 0


# ── Phase 2 — fusion normalization ────────────────────────────────────────

def test_normalize_sparse_scores_to_unit():
    chunks = [_chunk("a", score=139.9), _chunk("b", score=103.5), _chunk("c", score=120.0)]
    _normalize_scores_to_unit(chunks)
    assert all(0.0 <= c.score <= 1.0 for c in chunks)
    assert max(c.score for c in chunks) == 1.0
    assert chunks[0].score == 1.0


def test_normalize_empty_and_zero_are_safe():
    _normalize_scores_to_unit([])                       # no error on empty
    z = [_chunk("a", score=0.0), _chunk("b", score=0.0)]
    _normalize_scores_to_unit(z)                        # no div-by-zero
    assert all(c.score == 0.0 for c in z)


# ── Phase 3 — GERG query-ranked edges ─────────────────────────────────────

def test_edge_relevance_requires_subject_hit():
    groups = concept_groups("what is nlp and fine tuning")
    # definitional predicate but NO subject match -> 0 (the floodgate guard)
    assert _edge_query_relevance("machine learning", "JavaScript", "uses", groups) == 0
    # subject match, non-definitional -> 1
    assert _edge_query_relevance("NLP", "tokenization", "related_to", groups) == 1
    # subject match + definitional bonus -> 2
    assert _edge_query_relevance("NLP", "tokenization", "uses", groups) == 2
    # alias match on the neighbor counts too
    assert _edge_query_relevance("transformer", "natural language processing", "part_of", groups) == 2


def test_query_rank_keeps_subject_edges_drops_offtopic():
    rows = [
        {"seed_entity": "NLP", "neighbor_entity": "tokenization", "predicate": "uses", "edge_weight": 0.50},
        {"seed_entity": "machine learning", "neighbor_entity": "JavaScript", "predicate": "uses", "edge_weight": 0.99},
        {"seed_entity": "transformer", "neighbor_entity": "natural language processing", "predicate": "part_of", "edge_weight": 0.40},
        {"seed_entity": "OpenAI", "neighbor_entity": "Sam Altman", "predicate": "works_for", "edge_weight": 0.95},
    ]
    kept = {(r["seed_entity"], r["neighbor_entity"]) for r in _query_rank_rows(rows, "what is nlp and fine tuning", 8)}
    assert kept == {("NLP", "tokenization"), ("transformer", "natural language processing")}


def test_query_rank_empty_when_nothing_relevant():
    rows = [{"seed_entity": "OpenAI", "neighbor_entity": "Sam Altman", "predicate": "works_for", "edge_weight": 0.99}]
    assert _query_rank_rows(rows, "what is nlp", 8) == []   # correctly absent, not noise


def test_query_rank_generic_only_query_falls_back():
    rows = [{"seed_entity": "A", "neighbor_entity": "B", "predicate": "uses", "edge_weight": 0.9}]
    # purely generic query -> no non-generic anchor -> keep top_k by order (no over-prune)
    assert len(_query_rank_rows(rows, "the model system data process", 8)) == 1

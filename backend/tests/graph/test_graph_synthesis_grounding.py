import pytest

from services.graph import orchestrator
from services.graph.orchestrator import (
    _local_graph_structural_signals,
    _maybe_add_web_grounding_to_packet,
    _public_web_grounding_terms,
    _render_packet_user_prompt,
)


def test_local_graph_signals_surface_analogy_weak_link_transfer_and_seed_bridges():
    nodes = [
        {"id": "a", "display_name": "NLP", "domain_type": "AI", "mention_count": 8},
        {"id": "b", "display_name": "Data augmentation", "domain_type": "Data", "mention_count": 7},
        {"id": "c", "display_name": "Synthetic examples", "domain_type": "Method"},
        {"id": "d", "display_name": "Training data", "domain_type": "Dataset"},
        {"id": "e", "display_name": "Machine learning", "domain_type": "AI"},
        {"id": "x", "display_name": "Language model", "domain_type": "Model"},
    ]
    links = [
        {"source": "a", "target": "c", "predicate": "uses", "confidence": 0.9},
        {"source": "b", "target": "c", "predicate": "uses", "confidence": 0.9},
        {"source": "a", "target": "d", "predicate": "requires", "confidence": 0.8},
        {"source": "b", "target": "d", "predicate": "creates", "confidence": 0.8},
        {"source": "a", "target": "e", "predicate": "related_to", "confidence": 0.2},
        {"source": "e", "target": "x", "predicate": "supports", "confidence": 0.7},
    ]
    seeds = [
        {"entity_id": "a", "display_name": "NLP"},
        {"entity_id": "b", "display_name": "Data augmentation"},
    ]
    bridges = [
        {
            "entity_id": "x",
            "display_name": "Language model",
            "entity_type": "Model",
            "connected_seed_count": 1,
            "connected_seeds": ["a"],
            "source": "path_count",
        },
        {
            "entity_id": "global",
            "display_name": "Global hub",
            "connected_seed_count": 0,
            "connected_seeds": [],
            "source": "betweenness",
        },
    ]

    signals = _local_graph_structural_signals(
        nodes=nodes,
        links=links,
        seeds=seeds,
        gaps=[],
        bridges=bridges,
    )

    assert signals["analogies"], "shared seed-neighborhood structure should create analogies"
    assert any(item["source"] == "a" and item["target"] == "b" for item in signals["analogies"])
    assert signals["weak_links"], "generic/low-confidence local edges should create weak links"
    assert any(item["weakness_type"] == "generic_relation" for item in signals["weak_links"])
    assert signals["transfers"], "local hubs should create transfer candidates"
    assert signals["bridges"], "seed-connected bridges should survive"
    assert all(int(item.get("connected_seed_count") or 0) >= 1 for item in signals["bridges"])
    assert all(item.get("target") != "global" for item in signals["bridges"])
    assert signals["frontier"][0]["entity_id"] in {"a", "b"}


def test_public_web_terms_do_not_include_private_evidence_text():
    packet = {
        "anchors": ["NLP"],
        "entities": [{"canonical_name": "Data augmentation"}],
        "analogies": [{"source_name": "Synthetic examples", "target_name": "Training data"}],
        "evidence": [
            {
                "text": "PRIVATE CHUNK: this proprietary passage must never become a web query",
                "source_tier": "qdrant_mongo_graph",
            }
        ],
    }

    terms = _public_web_grounding_terms(packet)

    assert "NLP" in terms
    assert "Data augmentation" in terms
    joined = " ".join(terms)
    assert "PRIVATE CHUNK" not in joined
    assert "proprietary passage" not in joined


@pytest.mark.asyncio
async def test_web_grounding_disabled_by_default():
    packet = {"query": "what is NLP", "evidence": []}
    trace = {}

    await _maybe_add_web_grounding_to_packet(
        packet=packet,
        trace=trace,
        query="what is NLP",
        user_id=None,
        synthesis_mode="research",
        enabled=False,
        fetch_depth="normal",
        max_results=5,
    )

    assert packet.get("web_evidence") is None
    assert trace["web_grounding"] == {"enabled": False, "status": "disabled"}


@pytest.mark.asyncio
async def test_web_grounding_adds_separate_tagged_evidence(monkeypatch):
    async def fake_retrieve_web_grounding_evidence(**_kwargs):
        return [
            {
                "chunk_id": "web:abc",
                "doc_id": "https://example.test/nlp",
                "text": "Natural language processing is a field concerned with human language.",
                "summary": "NLP web overview.",
                "source_label": "Example NLP overview",
                "source_tier": "web_search",
                "metadata": {
                    "url": "https://example.test/nlp",
                    "source_type": "webpage",
                },
            }
        ], {"enabled": True, "status": "ok", "search_query": "NLP"}

    monkeypatch.setattr(
        orchestrator,
        "_retrieve_web_grounding_evidence",
        fake_retrieve_web_grounding_evidence,
    )
    packet = {
        "query": "what is NLP",
        "anchors": ["NLP"],
        "evidence": [
            {
                "evidence_id": "e1",
                "chunk_id": "local-1",
                "doc_id": "doc-1",
                "text": "Local corpus says NLP processes language.",
                "source_label": "Local doc",
                "source_tier": "qdrant_mongo_graph",
            }
        ],
    }
    trace = {}

    await _maybe_add_web_grounding_to_packet(
        packet=packet,
        trace=trace,
        query="what is NLP",
        user_id="user-1",
        synthesis_mode="research",
        enabled=True,
        fetch_depth="normal",
        max_results=5,
    )

    assert trace["web_grounding"]["status"] == "ok"
    assert trace["web_grounding"]["accepted_chunks"] == 1
    assert len(packet["web_evidence"]) == 1
    assert packet["web_evidence"][0]["source_tier"] == "web_search"
    assert packet["evidence"][-1]["evidence_id"] == "w1"

    prompt = _render_packet_user_prompt(packet, synthesis_mode="research")
    assert "[WEB]" in prompt
    assert "current external web evidence" in prompt

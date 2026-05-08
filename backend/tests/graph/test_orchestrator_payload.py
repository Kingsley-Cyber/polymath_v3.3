import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

from models.schemas import SourceChunk
from services.graph.analytics import CorpusMetrics, DomainCluster, DomainMap
from services.graph.analytics import QueryAnchor, VectorScopeResult, resolve_query_scope
from services.graph.overview import build_overview_graph
from services.graph.orchestrator import (
    _build_insight_packet,
    _build_subgraph_payload,
    _build_weak_links,
    _call_llm_synthesis,
    _compact_packet_for_prompt,
    _context_graph_from_result,
    _curated_evidence_rows,
    _deterministic_prose_fallback,
    _llm_context_trace_from_packet,
    _should_skip_synthesis,
    _source_docs_from_retrieval_chunks,
    _source_label_from_row,
    _sync_headline_from_auto_synthesis,
    _synthesis_sources_from_packet,
)


def _metrics() -> CorpusMetrics:
    return CorpusMetrics(
        corpus_id="c1",
        corpus_change_signature="sig",
        computed_at=datetime.utcnow(),
        node_count=2,
        edge_count=0,
        density=0.0,
        cross_domain_edge_pct=0.0,
        modularity_proxy=0.0,
        domain_density={},
        per_domain_edge_counts={},
        relation_family_counts={},
        top_pagerank=[],
        top_cross_domain_pagerank=[],
        node_domain_map={
            "product:identity-extraction-app": "Creative Coding Simulations",
            "concept:generative-ai": "Cymatics and Generative Art",
        },
        node_domains_touched={},
        frontier_candidates=[],
        fragile_bridges=[],
        terminological_gaps=[],
        structural_analogies=[],
        transfer_candidates=[],
        entity_name_map={
            "product:identity-extraction-app": "Identity Extraction App",
            "concept:generative-ai": "generative ai",
        },
        entity_concept_map={
            "product:identity-extraction-app": {
                "label": "Identity Extraction App / The Council",
                "degree": 4,
            },
            "concept:generative-ai": {
                "label": "perceptron / neural network",
                "degree": 8,
            },
        },
        entity_facet_map={
            "product:identity-extraction-app": {
                "object_kind": "App",
                "domain_type": "Feature",
                "canonical_family": "identity_extraction",
            },
            "concept:generative-ai": {
                "canonical_family": "generative_ai",
            },
        },
        ontology_version="2026-04-25-v1",
    )


def test_subgraph_payload_includes_working_entities_with_facets():
    payload = _build_subgraph_payload(
        _metrics(),
        frontier=[],
        analogies=[],
        bridges=[],
        transfers=[],
        working_entity_ids={
            "product:identity-extraction-app",
            "concept:generative-ai",
        },
    )

    nodes = {node["id"]: node for node in payload["nodes"]}
    assert nodes["product:identity-extraction-app"]["label"] == "Identity Extraction App"
    assert nodes["product:identity-extraction-app"]["object_kind"] == "App"
    assert nodes["product:identity-extraction-app"]["domain_type"] == "Feature"
    assert nodes["product:identity-extraction-app"]["canonical_family"] == "identity_extraction"
    assert nodes["concept:generative-ai"]["canonical_family"] == "generative_ai"


def test_subgraph_payload_includes_context_edges_without_overriding_special_edges():
    payload = _build_subgraph_payload(
        _metrics(),
        frontier=[],
        analogies=[
            {
                "source": "product:identity-extraction-app",
                "target": "concept:generative-ai",
                "source_name": "Identity Extraction App",
                "target_name": "generative ai",
                "source_domain": "Creative Coding Simulations",
                "target_domain": "Cymatics and Generative Art",
            }
        ],
        bridges=[],
        transfers=[],
        working_entity_ids={
            "product:identity-extraction-app",
            "concept:generative-ai",
        },
        context_links=[
            {
                "source": "product:identity-extraction-app",
                "target": "concept:generative-ai",
                "predicate": "uses",
                "relation_family": "Operational",
                "confidence": 0.9,
            }
        ],
    )

    assert len(payload["links"]) == 1
    assert payload["links"][0]["emphasis"] == "ghost_analogy"
    nodes = {node["id"]: node for node in payload["nodes"]}
    assert nodes["product:identity-extraction-app"]["emphasis"] == "analogy_anchor"


def test_weak_links_flag_single_path_bridges():
    weak_links = _build_weak_links(
        _metrics(),
        bridges=[
            {
                "source": "product:identity-extraction-app",
                "source_name": "Identity Extraction App",
                "source_domain": "Creative Coding Simulations",
                "target": "concept:generative-ai",
                "target_name": "generative ai",
                "target_domain": "Cymatics and Generative Art",
                "classification": "conceptual",
                "path_count": 1,
                "path_entity_ids": [
                    "product:identity-extraction-app",
                    "concept:generative-ai",
                ],
                "path_entities": ["Identity Extraction App", "generative ai"],
                "evidence": "Articulation edge: removing this route disconnects the bridge.",
            }
        ],
        analogies=[],
        context_links=[],
    )

    assert weak_links[0]["weakness_type"] == "fragile_bridge"
    assert weak_links[0]["severity"] == "high"
    assert "only 1 supporting path" in weak_links[0]["rationale"]


def test_context_graph_marks_topics_and_gap_edges():
    result = SimpleNamespace(
        concept_communities=[
            {
                "concept_id": "c0",
                "label": "Game Mechanics",
                "size": 5,
                "scope_count": 2,
                "top_entities": ["Roblox obby"],
            }
        ],
        entity_concept_map={
            "concept:roblox-obby": {"concept_id": "c0", "label": "Game Mechanics"}
        },
        themes=[{"theme_id": "c0", "name": "Game Mechanics"}],
        bridges_v2=[],
        gaps_v2=[{"cluster_a": "c0", "cluster_b": "c1"}],
        weak_links=[],
        graph={
            "nodes": [
                {
                    "id": "concept:roblox-obby",
                    "label": "Roblox obby",
                    "emphasis": "normal",
                    "degree": 3,
                },
                {
                    "id": "concept:movement",
                    "label": "Movement Loop",
                    "emphasis": "normal",
                    "degree": 2,
                }
            ],
            "links": [
                {
                    "source": "concept:roblox-obby",
                    "target": "concept:movement",
                    "emphasis": "gap_edge",
                    "classification": "structural_gap",
                }
            ],
        },
    )

    payload = _context_graph_from_result(result)

    assert any(node["kind"] == "topic" for node in payload["nodes"])
    assert any(link["suggested"] for link in payload["links"])


def test_deterministic_prose_fallback_describes_the_packet():
    """When the LLM is unreachable, the fallback should be honest prose, not card scaffolding."""

    result = SimpleNamespace(
        headline={"headline": "Database linearizability client"},
        interpretation="",
    )
    packet = {
        "query": "database linearizability client",
        "evidence": [
            {"doc_id": "doc-1", "chunk_id": "c1", "text": "linearizability proof", "evidence_id": "e1"},
        ],
        "communities": [{"label": "Distributed Systems"}],
        "edges": [
            {"source_name": "Riak", "target_name": "Cassandra", "predicate": "compared_to"},
        ],
        "gaps": [],
    }

    payload = _deterministic_prose_fallback(result, packet, "llm_request_failure")

    assert payload["fallback"] is True
    assert payload["fallback_reason"] == "llm_request_failure"
    assert "markdown" in payload
    assert payload["markdown"]
    # Prose should plainly say it's a fallback (italic underscore wrapper) so
    # the reader knows no model wrote the prose.
    assert "synthesis model" in payload["markdown"].lower()
    # No card schema fields should leak into the payload.
    for legacy_key in ("themes", "bridges", "gaps", "emerging_signals", "next_moves", "evidence_notes"):
        assert legacy_key not in payload


def test_overview_graph_builds_domain_and_concept_supernodes():
    metrics = _metrics()
    metrics.concept_communities = [
        {
            "concept_id": "c0",
            "label": "Identity Extraction App / generative ai",
            "size": 2,
            "top_entities": ["Identity Extraction App", "generative ai"],
            "member_ids": [
                "product:identity-extraction-app",
                "concept:generative-ai",
            ],
            "pagerank_sum": 0.42,
            "bridge_count": 1,
        }
    ]
    metrics.entity_concept_map["product:identity-extraction-app"]["concept_id"] = "c0"
    metrics.entity_concept_map["concept:generative-ai"]["concept_id"] = "c0"
    domain_map = DomainMap(
        corpus_id="c1",
        corpus_change_signature="sig",
        computed_at=datetime.utcnow(),
        doc_assignments={},
        clusters={
            0: DomainCluster(
                cluster_id=0,
                name="Creative Coding Simulations",
                size=2,
                top_entities=["Identity Extraction App"],
            )
        },
        outliers=[],
    )

    overview = build_overview_graph(domain_map, metrics)

    assert overview["view"] == "overview"
    assert any(node["entity_type"] == "domain" for node in overview["nodes"])
    assert any(node["entity_type"] == "concept_community" for node in overview["nodes"])
    assert any(edge["predicate"] == "contains" for edge in overview["edges"])


def test_skip_synthesis_allows_text_evidence_for_lookup_turns():
    metrics = _metrics()
    metrics.node_count = 50
    metrics.concept_communities = [
        {"concept_id": "c0", "label": "TensorFlow Lite / ML Kit", "size": 8}
    ]

    assert _should_skip_synthesis(metrics) is True
    assert _should_skip_synthesis(
        metrics,
        evidence_chunks=[{"chunk_id": "c1", "text": "TensorFlow Lite runs models on device."}],
        intent_profile={"intent_type": "lookup"},
    ) is False


@pytest.mark.asyncio
async def test_resolve_query_scope_reserves_budget_for_vector_evidence(monkeypatch):
    from services.graph import analytics

    anchors = [
        QueryAnchor(
            anchor_type="entity",
            anchor_id="entity:tensorflow-lite",
            label="TensorFlow Lite",
            score=0.95,
            source="entity_name",
        )
    ]
    anchor_seen: dict[str, int] = {}
    vector_seen: dict[str, int] = {}

    async def fake_resolve_anchors(*_args, **_kwargs):
        return anchors

    async def fake_anchor_scope(*_args, entity_cap: int, **_kwargs):
        anchor_seen["cap"] = entity_cap
        return {f"anchor:{i}" for i in range(entity_cap)}

    async def fake_query_scope(*_args, entity_cap: int, **_kwargs):
        vector_seen["cap"] = entity_cap
        return VectorScopeResult(
            entity_ids={f"vector:{i}" for i in range(entity_cap)},
            chunk_refs=[{"chunk_id": "chunk:1", "text": "ML Kit supports on-device inference."}],
            entity_scores={f"vector:{i}": 1.0 for i in range(entity_cap)},
        )

    monkeypatch.setattr(analytics, "resolve_query_anchors", fake_resolve_anchors)
    monkeypatch.setattr(analytics, "_anchor_scope_entities", fake_anchor_scope)
    monkeypatch.setattr(analytics, "query_scope_details", fake_query_scope)

    scope = await resolve_query_scope(
        qdrant=None,
        neo4j_driver=object(),
        db=object(),
        corpus_id="c1",
        query="How do TensorFlow Lite and ML Kit support on-device machine learning?",
        entity_cap=50,
    )

    assert anchor_seen["cap"] == 30
    assert vector_seen["cap"] == 20
    assert len(scope.entity_ids) == 50
    assert scope.vector_entity_count == 20


# ────────────────────────────────────────────────────────────────────────────
# Auto-Synthesis packet + LLM call coverage (Phase: query-first synthesis)
# ────────────────────────────────────────────────────────────────────────────


def _packet_result_fixture(*, with_evidence: bool = True, with_temporal: bool = False):
    trace = {
        "stages": [
            {"stage": "seed", "label": "Seed", "count": 2, "status": "ok"},
            {"stage": "expanded", "label": "Expanded", "count": 4, "status": "ok"},
            {
                "stage": "working_set",
                "label": "Working set",
                "count": 2,
                "status": "ok",
            },
            {
                "stage": "selected_edges",
                "label": "Selected edges",
                "count": 1,
                "status": "ok",
            },
            {
                "stage": "source_docs",
                "label": "Source docs",
                "count": 1 if with_evidence else 0,
                "status": "ok",
            },
        ],
        "working_entities": [
            {
                "entity_id": "concept:roblox-obby",
                "name": "Roblox obby",
                "degree": 4,
                "domain": "Game Mechanics",
            },
            {
                "entity_id": "concept:movement",
                "name": "Movement Loop",
                "degree": 3,
                "domain": "Game Mechanics",
            },
        ],
        "selected_edges": [
            {
                "source": "concept:roblox-obby",
                "target": "concept:movement",
                "predicate": "uses",
                "relation_family": "Operational",
                "confidence": 0.92,
            }
        ],
        "source_docs": (
            [
                {
                    "chunk_id": "chunk:abc123",
                    "doc_id": "doc:obby_design",
                    "text": "The obby uses a checkpoint loop to teach movement before each onboarding gap.",
                    "source_label": "Obby Design Notes",
                    "source": {
                        "title": "Obby Design Notes",
                        "author": "Studio Team",
                        "source_type": "markdown",
                        "section": "Movement Loop",
                        "page_range": "2",
                        **({"publication_date": "2026-04-01"} if with_temporal else {}),
                    },
                }
            ]
            if with_evidence
            else []
        ),
    }
    return SimpleNamespace(
        anchors=[
            {"anchor_id": "concept:roblox-obby", "label": "Roblox obby"},
            {"anchor_id": "concept:movement", "label": "Movement Loop"},
        ],
        graph={
            "nodes": [
                {
                    "id": "concept:roblox-obby",
                    "label": "Roblox obby",
                    "degree": 4,
                    "domain": "Game Mechanics",
                    "domain_type": "Mechanic",
                    "object_kind": "Concept",
                    "canonical_family": "platformer",
                },
                {
                    "id": "concept:movement",
                    "label": "Movement Loop",
                    "degree": 3,
                    "domain": "Game Mechanics",
                    "domain_type": "Mechanic",
                    "object_kind": "Concept",
                    "canonical_family": "platformer",
                },
            ],
            "links": [],
        },
        concept_communities=[
            {
                "concept_id": "c0",
                "label": "Game Mechanics",
                "size": 6,
                "scope_count": 2,
                "bridge_count": 1,
                "top_entities": ["Roblox obby", "Movement Loop"],
            }
        ],
        entity_concept_map={
            "concept:roblox-obby": {"concept_id": "c0", "label": "Game Mechanics"},
            "concept:movement": {"concept_id": "c0", "label": "Game Mechanics"},
        },
        gaps_v2=[
            {
                "gap_id": "g0",
                "cluster_a": "c0",
                "cluster_b": "c1",
                "cluster_a_label": "Game Mechanics",
                "cluster_b_label": "Player Onboarding",
                "question": "How does onboarding chain into the obby movement loop?",
            }
        ],
        latent_topics=[
            {
                "entity_id": "concept:checkpoint",
                "canonical_name": "Checkpoint",
                "domain": "Game Mechanics",
                "mention_count": 6,
                "doc_count": 2,
                "degree": 2,
                "rationale": "Checkpoints recur across design notes without explicit cross-links.",
            }
        ],
        weak_links=[
            {
                "source": "concept:roblox-obby",
                "target": "concept:movement",
                "source_name": "Roblox obby",
                "target_name": "Movement Loop",
                "weakness_type": "fragile_bridge",
                "severity": "high",
                "rationale": "Only one supporting path between the two concepts.",
            }
        ],
        interpretation="Obby design hinges on a tight movement-checkpoint loop.",
        headline={"headline": "A movement-checkpoint loop carries the obby"},
        themes=[],
        bridges_v2=[],
        trace=trace,
    )


def test_insight_packet_caps_entities_and_carries_facets():
    result = _packet_result_fixture()
    packet = _build_insight_packet(result, query="How does the obby loop work?", corpus_id="c1")
    assert packet["query"].startswith("How does the obby loop work")
    assert packet["corpus_id"] == "c1"
    assert len(packet["entities"]) <= 20
    assert len(packet["edges"]) <= 30
    assert len(packet["evidence"]) <= 12

    obby = next(e for e in packet["entities"] if e["entity_id"] == "concept:roblox-obby")
    assert obby["domain_type"] == "Mechanic"
    assert obby["canonical_family"] == "platformer"
    assert packet["temporal_support"] is False
    assert packet["sparse"] is False
    assert any(stage["stage"] == "selected_edges" for stage in packet["trace_stages"])
    assert packet["evidence"][0]["source"]["author"] == "Studio Team"
    assert packet["evidence"][0]["source"]["section"] == "Movement Loop"
    assert packet["graph_hint"]["shape"]["label"] == "focused neighborhood"
    assert packet["graph_hint"]["gateways"][0]["name"] in {"Roblox obby", "Movement Loop"}
    assert packet["graph_hint"]["supporting_statements"][0]["evidence_id"] == "e1"


def test_compact_packet_uses_gateway_hint_not_strength_report():
    result = _packet_result_fixture()
    packet = _build_insight_packet(result, query="How does the obby loop work?", corpus_id="c1")
    compact = _compact_packet_for_prompt(packet)

    assert "graph_hint" in compact
    assert "gateway_focus" in compact
    assert "bridge_focus" not in compact
    assert all("strength" not in item for item in compact["gateway_focus"])
    assert "strong" not in str(compact["gateway_focus"]).lower()


def test_insight_packet_marks_sparse_when_evidence_missing():
    result = _packet_result_fixture(with_evidence=False)
    # Strip everything that would otherwise satisfy the sparse heuristic.
    result.anchors = []
    result.concept_communities = []
    result.graph = {"nodes": [], "links": []}
    result.trace["working_entities"] = []
    result.trace["selected_edges"] = []
    packet = _build_insight_packet(result, query="thin query", corpus_id="c1")
    assert packet["sparse"] is True
    assert packet["evidence"] == []


def test_curated_evidence_drops_bibliography_without_least_bad_fallback():
    evidence, rejected, temporal_support, rejection_reasons = _curated_evidence_rows(
        [
            {
                "chunk_id": "bib:page178",
                "doc_id": "doc:trading_strategies",
                "text": (
                    "Journal of Financial and Quantitative Analysis 33(1): "
                    "139-157. 177 Electronic copy available at: "
                    "https://ssrn.com/abstract=3247865"
                ),
                "source_label": "151 Trading Strategies.pdf",
                "heading_path": ["page_178"],
                "page_start": 178,
            }
        ],
        query="Journal of Finance centrality",
    )

    assert evidence == []
    assert rejected == 1
    assert temporal_support is False
    assert rejection_reasons["low_value_section"] == 1


def test_curated_evidence_keeps_low_overlap_non_structural_chunks():
    """The retriever's job is finding semantically relevant chunks even when
    they don't share query vocabulary. We trust its ranking — only structural
    disqualifiers (bibliography/index/front_matter) get dropped. Letting raw
    rerank score gate substance was the bug we deliberately fixed."""

    evidence, rejected, _temporal_support, _rejection_reasons = _curated_evidence_rows(
        [
            {
                "chunk_id": "chunk:weak",
                "doc_id": "doc:weak",
                "text": (
                    "The Council is a book club where the only member is the user, "
                    "and the AI has read every book they chose to bring."
                ),
                "source_label": "Product_Creation_Document.docx",
                "score": -2.7,
            }
        ],
        query="explaining journalism in AI",
    )

    assert len(evidence) == 1
    assert evidence[0]["chunk_id"] == "chunk:weak"
    assert rejected == 0


def test_retrieval_chunks_convert_to_capped_packet_source_docs():
    chunks = [
        SourceChunk(
            chunk_id=f"chunk:{idx}",
            parent_id=f"parent:{idx}",
            doc_id="doc:mlkit",
            corpus_id="c1",
            text=f"ML Kit evidence child chunk {idx}",
            summary=None,
            score=1.0 - (idx * 0.01),
            source_tier="retriever",
            doc_name="ML Kit Guide.pdf",
            heading_path=["Chapter 4", "Object Detection"],
            provenance=[{"retriever": "test"}],
        )
        for idx in range(8)
    ]

    rows = _source_docs_from_retrieval_chunks(chunks, max_chunks=6)

    assert len(rows) == 6
    assert rows[0]["retriever"] == "shared_chat_retriever"
    assert rows[0]["source_label"] == "ML Kit Guide.pdf"
    assert rows[0]["heading_path"] == ["Chapter 4", "Object Detection"]
    assert rows[-1]["chunk_id"] == "chunk:5"


def test_graph_receipts_prefer_filename_when_source_label_is_doc_hash():
    doc_id = "4497907efa2abcfd610d29e2c2f2ff9588a44a6a0c594d831d8646528ce26383"
    filename = "Designing Data-Intensive Applications - Martin Kleppmann.md"
    source_row = {
        "chunk_id": "chunk:linearizability",
        "doc_id": doc_id,
        "source_label": doc_id,
        "source": {"filename": filename, "title": doc_id},
        "text": "Linearizability makes a database appear as if there is only a single copy of the data.",
    }

    assert _source_label_from_row(source_row, doc={"filename": filename}, doc_id=doc_id) == filename

    packet = {
        "query": "database linearizability client",
        "collections": {},
        "retrieval": {},
        "graph_hint": {},
        "entities": [],
        "communities": [],
        "edges": [],
        "gaps": [],
        "signals": [],
        "weak_links": [],
        "evidence": [source_row],
        "evidence_filter": {},
        "temporal_support": False,
        "sparse": False,
    }
    receipt = _llm_context_trace_from_packet(packet)

    assert receipt["files"][0]["source_label"] == filename
    assert receipt["chunks"][0]["source_label"] == filename
    # Prompt rendering strips .md extensions so the model cites
    # "Designing Data-Intensive Applications" naturally.
    cleaned = filename.rsplit(".md", 1)[0]
    assert cleaned in receipt["prompt"]["preview"]
    assert doc_id not in receipt["prompt"]["preview"]

    result = SimpleNamespace(
        query="database linearizability client",
        trace={"source_docs": [source_row]},
        graph={"nodes": [], "links": []},
        entity_concept_map={},
        themes=[],
        bridges_v2=[],
        gaps_v2=[],
        weak_links=[],
    )
    context = _context_graph_from_result(result)

    assert any(
        node["kind"] == "document" and node["label"] == filename
        for node in context["nodes"]
    )


def test_insight_packet_quality_gate_withholds_bibliography_only_graph_context():
    result = _packet_result_fixture()
    result.trace["source_docs"] = [
        {
            "chunk_id": "bib:page178",
            "doc_id": "doc:trading_strategies",
            "text": (
                "Journal of Financial and Quantitative Analysis 33(1): "
                "139-157. 177 Electronic copy available at: "
                "https://ssrn.com/abstract=3247865"
            ),
            "source_label": "151 Trading Strategies.pdf",
            "heading_path": ["page_178"],
            "page_start": 178,
        }
    ]

    packet = _build_insight_packet(
        result,
        query="Journal of Finance centrality in Cluster 0",
        corpus_id="c1",
    )
    compact = _compact_packet_for_prompt(packet)

    assert packet["evidence"] == []
    assert packet["evidence_filter"]["all_rejected"] is True
    assert packet["sparse"] is True
    assert compact["evidence"] == []
    assert compact["groups"] == []
    assert compact["edges"] == []
    assert compact["gaps"] == []
    assert compact["signals"] == []
    assert "graph context withheld" in compact["quality_gate"]


def test_context_graph_hides_working_set_when_evidence_gate_rejects_all():
    result = _packet_result_fixture()
    result.trace["source_docs"] = []
    result.trace["evidence_filter"] = {
        "raw": 1,
        "accepted": 0,
        "rejected": 1,
        "all_rejected": True,
    }

    context_graph = _context_graph_from_result(result)

    assert context_graph["nodes"] == []
    assert context_graph["links"] == []
    assert context_graph["meta"]["evidence_gate"] == "all_candidate_chunks_failed_quality_filter"


def test_insight_packet_detects_temporal_support():
    result = _packet_result_fixture(with_evidence=True, with_temporal=True)
    packet = _build_insight_packet(result, query="when did the obby ship?", corpus_id="c1")
    assert packet["temporal_support"] is True


def test_compact_packet_prompt_includes_source_metadata_without_ingest_dates():
    result = _packet_result_fixture(with_evidence=True, with_temporal=True)
    packet = _build_insight_packet(result, query="how does checkpoint support the obby movement loop?", corpus_id="c1")
    compact = _compact_packet_for_prompt(packet)
    source = compact["evidence"][0]["source"]
    # Prompt-side label is cleaned and gets an "(Author, Year)" suffix when
    # document metadata supplies them, so the model can cite naturally.
    assert source["title"].startswith("Obby Design Notes")
    assert "(Team, 2026)" in source["title"] or "Studio Team" in source.get("author", "")
    assert source["author"] == "Studio Team"
    assert source["date"] == "2026-04-01"
    assert "created_at" not in source
    assert "updated_at" not in source
    assert compact["synthesis_priority"]["primary"] == ["bridges", "gaps", "emerging_signals"]
    assert isinstance(compact["gaps"][0], dict)
    assert compact["gaps"][0]["q"].startswith("How does onboarding")
    assert "why" in compact["signals"][0]
    assert "docs" not in compact["signals"][0]
    assert "mentions" not in compact["signals"][0]


def _empty_packet(**overrides):
    base = {
        "sparse": False,
        "temporal_support": False,
        "query": "q",
        "corpus_id": "c1",
        "entities": [],
        "edges": [],
        "communities": [],
        "evidence": [],
        "anchors": [],
        "trace_stages": [],
        "gaps": [],
        "signals": [],
        "weak_links": [],
        "interpretation": "",
        "headline": "",
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_call_llm_synthesis_returns_prose_with_inline_citations(monkeypatch):
    """The synthesis call must return woven Markdown prose, not card JSON."""

    from services.graph import orchestrator as orch_mod

    prose = (
        "# Movement loop carries the obby\n\n"
        "The corpus shows a tight checkpoint cadence around the obby's first "
        "minute, with new players cycling through three deaths before the loop "
        "stabilizes [1]. Across the same scope, the graph suggests a bridge "
        "to onboarding pacing — a structural read, not a confirmed claim [2].\n\n"
        "A testable read: longer respawn windows would weaken the loop's grip."
    )

    class _StubLLM:
        async def complete_sync(self, **_kwargs):
            return prose

    fake_module = SimpleNamespace(llm_service=_StubLLM())
    monkeypatch.setitem(sys.modules, "services.llm", fake_module)

    payload, reason = await orch_mod._call_llm_synthesis(
        _empty_packet(
            evidence=[
                {"evidence_id": "e1", "doc_id": "d1", "chunk_id": "c1", "text": "checkpoint loop"},
                {"evidence_id": "e2", "doc_id": "d2", "chunk_id": "c2", "text": "onboarding pacing"},
            ]
        ),
        model_override=None,
    )

    assert reason is None
    assert payload is not None
    assert payload["fallback"] is False
    assert payload["headline"].startswith("Movement loop")
    assert "checkpoint cadence" in payload["markdown"].lower()
    # The headline line was lifted out of the markdown body.
    assert "# Movement loop" not in payload["markdown"]
    # Inline [1] [2] citations resolved to source receipts in citation order.
    assert [s["index"] for s in payload["sources"]] == [1, 2]
    assert payload["sources"][0]["evidence_id"] == "e1"
    # No card-shape leakage.
    for legacy_key in ("themes", "bridges", "gaps", "emerging_signals", "next_moves", "evidence_notes"):
        assert legacy_key not in payload


@pytest.mark.asyncio
async def test_call_llm_synthesis_calls_model_even_when_packet_is_sparse(monkeypatch):
    from services.graph import orchestrator as orch_mod

    calls = {"count": 0}
    prose = (
        "# Sparse read\n\n"
        "The packet is thin and the corpus has not surfaced enough chunks to "
        "anchor a confident analysis. The graph layer suggests a single "
        "neighborhood worth probing further."
    )

    class _StubLLM:
        async def complete_sync(self, **_kwargs):
            calls["count"] += 1
            return prose

    fake_module = SimpleNamespace(llm_service=_StubLLM())
    monkeypatch.setitem(sys.modules, "services.llm", fake_module)

    payload, reason = await orch_mod._call_llm_synthesis(
        _empty_packet(sparse=True, query="thin query"),
        model_override=None,
    )

    assert calls["count"] == 1
    assert reason is None
    assert payload is not None
    assert payload["fallback"] is False
    assert "sparse" in payload["markdown"].lower() or "thin" in payload["markdown"].lower()


@pytest.mark.asyncio
async def test_call_llm_synthesis_resolves_pool_model_credentials(monkeypatch):
    from services.graph import orchestrator as orch_mod
    from services import query_model_resolver as resolver_mod

    calls: dict[str, object] = {}

    async def _resolve_by_entry_id(user_id, entry_id):
        calls["resolved"] = (user_id, entry_id)
        return {
            "model": "openai/mistral-large-latest",
            "api_base": "https://api.mistral.ai/v1",
            "api_key": "test-key",
            "extra_params": {"custom": "value"},
        }

    class _StubLLM:
        async def complete_sync(self, **kwargs):
            calls["llm_kwargs"] = kwargs
            return "# Mistral resolved\n\nProse body here."

    monkeypatch.setattr(resolver_mod, "resolve_by_entry_id", _resolve_by_entry_id)
    fake_module = SimpleNamespace(llm_service=_StubLLM())
    monkeypatch.setitem(sys.modules, "services.llm", fake_module)

    payload, reason = await orch_mod._call_llm_synthesis(
        _empty_packet(),
        model_override="pool:mistral-entry",
        user_id="user-1",
    )

    assert reason is None
    assert payload is not None
    assert calls["resolved"] == ("user-1", "mistral-entry")
    llm_kwargs = calls["llm_kwargs"]
    assert llm_kwargs["model"] == "openai/mistral-large-latest"
    assert llm_kwargs["api_base"] == "https://api.mistral.ai/v1"
    assert llm_kwargs["api_key"] == "test-key"
    assert llm_kwargs["extra_params"]["custom"] == "value"
    # Prose contract: model-agnostic, no JSON response format coercion.
    assert "response_format" not in llm_kwargs["extra_params"]


@pytest.mark.asyncio
async def test_call_llm_synthesis_returns_empty_failure_on_blank_output(monkeypatch):
    """No JSON repair retry: empty output is a clean fallback signal."""

    from services.graph import orchestrator as orch_mod

    class _StubLLM:
        async def complete_sync(self, **_kwargs):
            return "   "

    fake_module = SimpleNamespace(llm_service=_StubLLM())
    monkeypatch.setitem(sys.modules, "services.llm", fake_module)

    payload, reason = await orch_mod._call_llm_synthesis(
        _empty_packet(),
        model_override=None,
    )

    assert payload is None
    assert reason == "llm_empty_response"


def test_synthesis_sources_only_includes_cited_indexes():
    packet = {
        "evidence": [
            {"evidence_id": "e1", "doc_id": "d1", "chunk_id": "c1", "text": "alpha"},
            {"evidence_id": "e2", "doc_id": "d2", "chunk_id": "c2", "text": "beta"},
            {"evidence_id": "e3", "doc_id": "d3", "chunk_id": "c3", "text": "gamma"},
        ]
    }

    sources = _synthesis_sources_from_packet(packet, "Cited [1] and [3] only.")

    assert [s["index"] for s in sources] == [1, 3]
    assert sources[0]["evidence_id"] == "e1"
    assert sources[1]["evidence_id"] == "e3"


def test_context_graph_node_carries_jump_metadata():
    result = SimpleNamespace(
        concept_communities=[
            {
                "concept_id": "c0",
                "label": "Game Mechanics",
                "size": 5,
                "scope_count": 2,
                "top_entities": ["Roblox obby"],
            }
        ],
        entity_concept_map={
            "concept:roblox-obby": {"concept_id": "c0", "label": "Game Mechanics"}
        },
        themes=[{"theme_id": "c0", "name": "Game Mechanics"}],
        bridges_v2=[],
        gaps_v2=[{"cluster_a": "c0", "cluster_b": "c1"}],
        weak_links=[],
        graph={
            "nodes": [
                {
                    "id": "concept:roblox-obby",
                    "label": "Roblox obby",
                    "emphasis": "normal",
                    "degree": 3,
                }
            ],
            "links": [],
        },
    )
    payload = _context_graph_from_result(result)
    concept_node = next(n for n in payload["nodes"] if n["kind"] == "concept")
    sections = {jump["section"] for jump in concept_node["jump_targets"]}
    # concept must offer a theme jump (cluster match) AND a gap jump (cluster
    # appears in gaps_v2). Multi-role concepts retain ALL targets.
    assert "themes" in sections
    assert "gaps" in sections


def test_context_graph_marks_gap_edges_as_suggested_only():
    result = SimpleNamespace(
        concept_communities=[],
        entity_concept_map={},
        themes=[],
        bridges_v2=[],
        gaps_v2=[],
        weak_links=[],
        graph={
            "nodes": [
                {"id": "a", "label": "A", "emphasis": "normal"},
                {"id": "b", "label": "B", "emphasis": "normal"},
            ],
            "links": [
                {
                    "source": "a",
                    "target": "b",
                    "emphasis": "gap_edge",
                    "classification": "structural_gap",
                }
            ],
        },
    )
    payload = _context_graph_from_result(result)
    gap_link = next(link for link in payload["links"] if link.get("source") == "a")
    assert gap_link["suggested"] is True


def test_context_graph_uses_query_scoped_groups_not_corpus_buckets():
    result = SimpleNamespace(
        concept_communities=[
            {
                "concept_id": "global",
                "label": "Global Corpus Bucket",
                "size": 999,
                "scope_count": 0,
                "top_entities": ["Unrelated"],
            }
        ],
        entity_concept_map={},
        themes=[],
        bridges_v2=[],
        gaps_v2=[],
        weak_links=[],
        anchors=[],
        latent_topics=[],
        interpretation="",
        headline={},
        trace={
            "working_entities": [{"entity_id": "a", "name": "Alpha"}],
            "selected_edges": [],
            "source_docs": [
                {
                    "chunk_id": "chunk:one",
                    "doc_id": "doc:one",
                    "source_label": "Doc One",
                    "text": "Alpha appears in this query-specific evidence chunk.",
                }
            ],
        },
        graph={
            "nodes": [
                {"id": "a", "label": "Alpha", "domain": "Query Domain", "degree": 2}
            ],
            "links": [],
        },
    )
    context = _context_graph_from_result(result)
    topic_labels = {node["label"] for node in context["nodes"] if node["kind"] == "topic"}
    assert context["meta"]["corpus_bucketed"] is False
    assert context["meta"]["topic_source"] == "query_scoped_concept_neighborhoods"
    assert "Global Corpus Bucket" not in topic_labels
    assert "Query Domain" in topic_labels
    assert any(node["kind"] == "document" and node["label"] == "Doc One" for node in context["nodes"])

    packet = _build_insight_packet(result, query="alpha", corpus_id="c1")
    packet_labels = {group["label"] for group in packet["communities"]}
    assert "Global Corpus Bucket" not in packet_labels
    assert "Query Domain" in packet_labels


def test_packet_filters_unrelated_vector_scope_neighborhoods():
    result = SimpleNamespace(
        query="Explore the ML Kit ImageView concept neighborhood around ML Kit and its cross-domain bridges.",
        concept_communities=[],
        entity_concept_map={
            "entity:ml-kit": {"concept_id": "c0", "label": "ML Kit MLKit ImageView"},
            "entity:android": {"concept_id": "c0", "label": "ML Kit MLKit ImageView"},
            "entity:option": {"concept_id": "c4", "label": "option strategy cross-border tax"},
            "entity:call-option": {"concept_id": "c4", "label": "option strategy cross-border tax"},
        },
        themes=[
            {"theme_id": "c0", "name": "ML Kit MLKit ImageView"},
            {"theme_id": "c4", "name": "option strategy cross-border tax"},
        ],
        bridges_v2=[],
        gaps_v2=[
            {
                "gap_id": "finance-gap",
                "cluster_a": "c4",
                "cluster_b": "c9",
                "question": "What connects options to bond immunization?",
            },
            {
                "gap_id": "ml-gap",
                "cluster_a": "c0",
                "cluster_b": "c23",
                "question": "What connects ML Kit to CameraX?",
            },
        ],
        latent_topics=[],
        weak_links=[
            {
                "source": "entity:call-option",
                "target": "entity:option",
                "weakness_type": "thin_evidence",
                "rationale": "Finance-only edge.",
            }
        ],
        anchors=[],
        interpretation="",
        headline={},
        trace={
            "working_entities": [
                {"entity_id": "entity:ml-kit", "name": "ML Kit"},
                {"entity_id": "entity:android", "name": "Android"},
                {"entity_id": "entity:option", "name": "option"},
                {"entity_id": "entity:call-option", "name": "call option"},
            ],
            "selected_edges": [
                {
                    "source": "entity:android",
                    "target": "entity:ml-kit",
                    "predicate": "runs_on",
                },
                {
                    "source": "entity:call-option",
                    "target": "entity:option",
                    "predicate": "references",
                },
            ],
            "source_docs": [
                {
                    "chunk_id": "mlkit:1",
                    "doc_id": "doc:mlkit",
                    "source_label": "ML Kit Guide",
                    "text": "ML Kit runs on Android and is commonly paired with CameraX for image labeling.",
                }
            ],
        },
        graph={
            "nodes": [
                {"id": "entity:ml-kit", "label": "ML Kit", "concept": "ML Kit MLKit ImageView", "degree": 20},
                {"id": "entity:android", "label": "Android", "concept": "ML Kit MLKit ImageView", "degree": 10},
                {"id": "entity:option", "label": "option", "concept": "option strategy cross-border tax", "degree": 34},
                {"id": "entity:call-option", "label": "call option", "concept": "option strategy cross-border tax", "degree": 11},
            ],
            "links": [
                {"source": "entity:android", "target": "entity:ml-kit", "predicate": "runs_on"},
                {"source": "entity:call-option", "target": "entity:option", "predicate": "references"},
            ],
        },
    )

    packet = _build_insight_packet(result, query=result.query, corpus_id="c1")
    labels = {group["label"] for group in packet["communities"]}
    edge_names = {(edge["source_name"], edge["target_name"]) for edge in packet["edges"]}
    assert "ML Kit MLKit ImageView" in labels
    assert "option strategy cross-border tax" not in labels
    assert ("call option", "option") not in edge_names
    assert [gap["gap_id"] for gap in packet["gaps"]] == ["ml-gap"]
    assert packet["gaps"][0]["support_status"] == "off_scope_terms_supported_by_evidence"
    assert packet["weak_links"] == []
    assert [signal["canonical_name"] for signal in packet["signals"]] == ["ML Kit MLKit ImageView"]
    assert packet["signals"][0]["domain"] == "query_scope"

    compact = _compact_packet_for_prompt(packet)
    assert compact["research_contract"]["claim_levels"] == [
        "observed evidence",
        "graph structure",
        "testable hypothesis",
    ]
    assert compact["gaps"][0]["support"] == "off_scope_terms_supported_by_evidence"

    context = _context_graph_from_result(result)
    topic_labels = {node["label"] for node in context["nodes"] if node["kind"] == "topic"}
    assert "ML Kit MLKit ImageView" in topic_labels
    assert "option strategy cross-border tax" not in topic_labels


def test_packet_drops_cross_domain_gaps_without_evidence_support():
    result = SimpleNamespace(
        query="Explore the database linearizability client concept neighborhood around database, linearizability, client and its cross-domain bridges.",
        concept_communities=[],
        entity_concept_map={
            "entity:database": {"concept_id": "c0", "label": "database linearizability client"},
            "entity:client": {"concept_id": "c0", "label": "database linearizability client"},
            "entity:rorschach": {"concept_id": "c9", "label": "RORSCHACH TAT projective techniques"},
        },
        themes=[
            {"theme_id": "c0", "name": "database linearizability client"},
            {"theme_id": "c9", "name": "RORSCHACH TAT projective techniques"},
        ],
        bridges_v2=[],
        gaps_v2=[
            {
                "gap_id": "psych-gap",
                "cluster_a": "c0",
                "cluster_b": "c9",
                "cluster_a_label": "database linearizability client",
                "cluster_b_label": "RORSCHACH TAT projective techniques",
                "question": "What connects database linearizability client to RORSCHACH TAT projective techniques through life course, depression?",
                "coherence": {"shared_terms": ["life course", "depression"]},
                "anchor_concepts": ["database", "linearizability", "RORSCHACH", "depression"],
            }
        ],
        latent_topics=[],
        weak_links=[],
        anchors=[],
        interpretation="",
        headline={},
        trace={
            "working_entities": [
                {"entity_id": "entity:database", "name": "database"},
                {"entity_id": "entity:client", "name": "client"},
                {"entity_id": "entity:rorschach", "name": "RORSCHACH"},
            ],
            "selected_edges": [
                {
                    "source": "entity:database",
                    "target": "entity:client",
                    "predicate": "uses",
                }
            ],
            "source_docs": [
                {
                    "chunk_id": "db:1",
                    "doc_id": "doc:db",
                    "source_label": "Designing Data-Intensive Applications.md",
                    "text": "Linearizability lets a database client observe the system as if there were a single copy of the data.",
                }
            ],
        },
        graph={
            "nodes": [
                {"id": "entity:database", "label": "database", "concept": "database linearizability client", "degree": 10},
                {"id": "entity:client", "label": "client", "concept": "database linearizability client", "degree": 5},
                {"id": "entity:rorschach", "label": "RORSCHACH", "concept": "RORSCHACH TAT projective techniques", "degree": 7},
            ],
            "links": [
                {"source": "entity:database", "target": "entity:client", "predicate": "uses"},
            ],
        },
    )

    packet = _build_insight_packet(result, query=result.query, corpus_id="c1")

    assert packet["gaps"] == []


def test_auto_synthesis_headline_sync_mirrors_prose_headline_onto_legacy_field():
    """The headline from the prose payload is mirrored onto result.headline for legacy callers."""

    result = SimpleNamespace(
        headline={"headline": "database linearizability client: stale legacy headline"},
        auto_synthesis={
            "headline": "database linearizability client: research read across Riak",
            "markdown": "# database linearizability client: research read across Riak\n\nProse...",
            "sources": [],
        },
    )

    _sync_headline_from_auto_synthesis(result)

    assert result.headline["headline"] == "database linearizability client: research read across Riak"

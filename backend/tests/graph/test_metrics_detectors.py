"""
P2 verification — metrics + insight detectors.

Unit tests build tiny synthetic NetworkX graphs and assert detector outputs.
Integration test runs the full compute_all_metrics() pipeline against the
live stack (skipped without `-m integration`).
"""
from __future__ import annotations

import os
from datetime import datetime

import numpy as np
import pytest

from services.graph.analytics import (
    CorpusMetrics,
    DomainMap,
    build_entity_facet_map,
    compute_cd_pagerank,
    compute_cross_domain_edge_pct,
    compute_domain_density,
    compute_pagerank,
    compute_per_domain_edge_counts,
    compute_relation_family_counts,
    detect_analogies_and_terminological_gaps,
    detect_fragile_bridges,
    detect_frontier_nodes,
    detect_transfer_opportunities,
    expand_scope_with_query_facets,
    neighbor_jaccard,
    resolve_metrics_ontology_version,
    topology_fingerprint,
    topology_similarity,
)


def _toy_graph():
    """Build a small multi-domain graph:
    - 3 "Flutter" nodes densely connected
    - 3 "Bayesian" nodes densely connected
    - 2 "Journaling" nodes
    - Two bridges: flutter-A <-> bayes-A (single path), journal-A <-> bayes-B
    - One frontier: cog-load (degree 2) touching flutter + bayes
    """
    import networkx as nx
    G = nx.Graph()

    flutter = ["f1", "f2", "f3"]
    bayes = ["b1", "b2", "b3"]
    journ = ["j1", "j2"]
    frontier = ["cog_load"]
    all_nodes = flutter + bayes + journ + frontier
    domain_map = {}
    for n in flutter:
        G.add_node(n, canonical_name=n, entity_type="Concept")
        domain_map[n] = "Flutter"
    for n in bayes:
        G.add_node(n, canonical_name=n, entity_type="Concept")
        domain_map[n] = "Bayesian"
    for n in journ:
        G.add_node(n, canonical_name=n, entity_type="Concept")
        domain_map[n] = "Journaling"
    for n in frontier:
        G.add_node(n, canonical_name=n, entity_type="Concept")
        domain_map[n] = "Cognitive"

    # Dense within-domain
    for i, a in enumerate(flutter):
        for b in flutter[i + 1:]:
            G.add_edge(a, b, predicate="part_of")
    for i, a in enumerate(bayes):
        for b in bayes[i + 1:]:
            G.add_edge(a, b, predicate="part_of")
    G.add_edge("j1", "j2", predicate="part_of")

    # Cross-domain bridges
    G.add_edge("f1", "b1", predicate="related_to")   # flutter <-> bayes
    G.add_edge("j1", "b2", predicate="related_to")   # journaling <-> bayes

    # Frontier node touching two domains
    G.add_edge("cog_load", "f2", predicate="related_to")
    G.add_edge("cog_load", "b3", predicate="related_to")

    touched = {n: [domain_map[n]] for n in all_nodes}
    touched["cog_load"] = ["Cognitive", "Flutter", "Bayesian"]
    return G, domain_map, touched


def test_cross_domain_edge_pct():
    G, dm, _ = _toy_graph()
    pct = compute_cross_domain_edge_pct(G, dm)
    # 4 cross-domain edges (f1-b1, j1-b2, cog_load-f2, cog_load-b3) out of 11 total
    assert 30 <= pct <= 40, f"expected ~36%, got {pct}"


def test_per_domain_edge_counts():
    G, dm, _ = _toy_graph()
    counts = compute_per_domain_edge_counts(G, dm)
    assert "Flutter" in counts
    assert counts["Flutter"]["internal"] == 3   # f1-f2, f1-f3, f2-f3
    assert counts["Flutter"]["external"] >= 1


def test_relation_family_counts_reads_folded_edge_families():
    G, _, _ = _toy_graph()
    G["f1"]["b1"]["relation_families"] = ["WeakAssociation", "Operational"]

    counts = compute_relation_family_counts(G)

    assert counts["WeakAssociation"] >= 1
    assert counts["Operational"] == 1


def test_domain_density_per_domain():
    G, dm, _ = _toy_graph()
    densities = compute_domain_density(G, dm)
    # Flutter subgraph is a 3-node clique → density = 1.0
    assert densities["Flutter"] == 1.0
    # Bayesian subgraph is also a 3-node clique → density = 1.0
    assert densities["Bayesian"] == 1.0


def test_cd_pagerank_rewards_domain_spanning():
    G, dm, touched = _toy_graph()
    pr = compute_pagerank(G)
    cd = compute_cd_pagerank(pr, touched)
    # cog_load touches 3 domains so its cd score should be at least 3× its pr
    assert cd["cog_load"] >= 3 * pr["cog_load"] - 1e-9


def test_topology_fingerprint_and_similarity():
    G, dm, _ = _toy_graph()
    fp_f1 = topology_fingerprint(G, "f1", dm)
    fp_b1 = topology_fingerprint(G, "b1", dm)
    assert fp_f1["degree"] == 3
    assert fp_b1["degree"] == 3
    sim = topology_similarity(fp_f1, fp_b1)
    assert 0.7 <= sim <= 1.0, f"similar-shaped nodes should score high, got {sim}"


def test_frontier_detector_finds_cog_load():
    G, dm, touched = _toy_graph()
    frontier = detect_frontier_nodes(G, dm, touched)
    ids = {f["entity_id"] for f in frontier}
    assert "cog_load" in ids
    cog = next(f for f in frontier if f["entity_id"] == "cog_load")
    assert cog["degree"] == 2
    assert len(cog["domains_touched"]) >= 2


def test_fragile_bridge_detector():
    G, dm, _ = _toy_graph()
    fragile = detect_fragile_bridges(G, dm)
    # The f1<->b1 edge is cross-domain; j1<->b2 is cross-domain.
    # Both have some alternate paths via cog_load, but path_count should be low.
    assert len(fragile) >= 1
    for fb in fragile:
        assert fb["source_domain"] != fb["target_domain"]


def test_analogy_detector_runs_on_toy_graph():
    """Analogies require topology_sim > 0.85 which is strict; toy graph
    may not trigger it, but the detector must at least run without error."""
    G, dm, _ = _toy_graph()
    fps = {n: topology_fingerprint(G, n, dm) for n in G.nodes}
    analogies, terminological = detect_analogies_and_terminological_gaps(G, dm, fps)
    assert isinstance(analogies, list)
    assert isinstance(terminological, list)


def test_transfer_detector_requires_analogs():
    G, dm, touched = _toy_graph()
    pr = compute_pagerank(G)
    cd = compute_cd_pagerank(pr, touched)
    # Empty analogies → zero transfer candidates
    transfers = detect_transfer_opportunities(G, dm, cd, [])
    assert transfers == []


def test_neighbor_jaccard():
    G, dm, _ = _toy_graph()
    # f1 neighbors: {f2, f3, b1}; f2 neighbors: {f1, f3, cog_load}
    # overlap = {f3}, union = {f1, f2, f3, b1, cog_load} → jac = 1/5 = 0.2
    jac = neighbor_jaccard(G, "f1", "f2")
    assert 0.15 <= jac <= 0.25


def test_entity_facet_map_ignores_version_only_nodes():
    import networkx as nx

    G = nx.Graph()
    G.add_node("version_only", ontology_version="2026-04-25-v1")
    G.add_node(
        "box2d",
        object_kind="Library",
        object_kind_parent="CodeArtifact",
        canonical_family="physics_simulation",
        ontology_version="2026-04-25-v1",
    )
    G.add_node(
        "book_json",
        domain_type="DataObject",
        domain_type_parent="ProductData",
        canonical_family="book_generation",
        ontology_version="2026-04-25-v1",
    )

    assert resolve_metrics_ontology_version(G) == "2026-04-25-v1"
    facets = build_entity_facet_map(G)
    assert "version_only" not in facets
    assert facets["box2d"]["object_kind"] == "Library"
    assert facets["box2d"]["canonical_family"] == "physics_simulation"
    assert facets["book_json"]["domain_type"] == "DataObject"


def test_query_facet_expansion_uses_cached_family_and_kind():
    metrics = CorpusMetrics(
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
        node_domain_map={},
        node_domains_touched={},
        frontier_candidates=[],
        fragile_bridges=[],
        terminological_gaps=[],
        structural_analogies=[],
        transfer_candidates=[],
        entity_concept_map={
            "box2d": {"pagerank": 0.02, "degree": 8},
            "report": {"pagerank": 0.01, "degree": 2},
        },
        entity_facet_map={
            "box2d": {
                "object_kind": "Library",
                "canonical_family": "physics_simulation",
            },
            "report": {
                "object_kind": "Report",
                "canonical_family": "app_architecture",
            },
            "book_json": {
                "domain_type": "DataObject",
                "canonical_family": "book_generation",
            },
        },
        ontology_version="2026-04-25-v1",
    )

    expanded = expand_scope_with_query_facets(
        metrics, set(), "show me physics simulation libraries", entity_cap=5
    )

    assert "box2d" in expanded
    assert "report" not in expanded

    expanded_prd = expand_scope_with_query_facets(
        metrics, set(), "show the book json data object", entity_cap=5
    )
    assert "book_json" in expanded_prd


# ── Integration ────────────────────────────────────────────────────────────

@pytest.mark.integration
@pytest.mark.asyncio
async def test_compute_all_metrics_integration():
    corpus_id = os.environ.get("POLYMATH_TEST_CORPUS_ID")
    if not corpus_id:
        pytest.skip("POLYMATH_TEST_CORPUS_ID not set")

    from motor.motor_asyncio import AsyncIOMotorClient
    from qdrant_client import AsyncQdrantClient
    from neo4j import AsyncGraphDatabase

    from config import get_settings
    from services.graph.analytics import compute_all_metrics, emerge_domains

    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    db = mongo[settings.MONGODB_DATABASE]
    qdrant = AsyncQdrantClient(url=settings.QDRANT_URL)
    neo4j = None
    if getattr(settings, "NEO4J_ENABLED", False):
        neo4j = AsyncGraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )

    try:
        domain_map = await emerge_domains(qdrant, neo4j, db, corpus_id, force=True)
        metrics = await compute_all_metrics(neo4j, db, corpus_id, domain_map, force=True)
        assert metrics.corpus_id == corpus_id
        # Either the graph is empty (no Neo4j data for this corpus) OR we got real stats.
        assert metrics.node_count >= 0
        assert 0 <= metrics.cross_domain_edge_pct <= 100
        # Re-run hits cache
        metrics2 = await compute_all_metrics(neo4j, db, corpus_id, domain_map, force=False)
        assert metrics2.corpus_change_signature == metrics.corpus_change_signature
    finally:
        await qdrant.close()
        if neo4j:
            await neo4j.close()
        mongo.close()

from datetime import datetime

from services.graph.analytics import (
    METRICS_CACHE_CONCEPT_LIMIT,
    METRICS_CACHE_MAP_ENTITY_LIMIT,
    CorpusMetrics,
    _deserialize_metrics,
    _serialize_metrics,
)


def test_metrics_cache_serialization_compacts_large_entity_maps():
    concept_communities = [
        {
            "concept_id": f"c{i}",
            "label": f"Concept {i}",
            "size": 200,
            "top_entities": [f"Entity {i}"],
            "top_entity_ids": [f"entity:{i}:top"],
            "member_ids": [f"entity:{i}:{j}" for j in range(200)],
            "pagerank_sum": 1.0,
            "bridge_count": 0,
        }
        for i in range(METRICS_CACHE_CONCEPT_LIMIT + 25)
    ]
    node_domain_map = {
        f"entity:{i}:{j}": "Cluster 0"
        for i in range(METRICS_CACHE_CONCEPT_LIMIT + 25)
        for j in range(200)
    }
    node_domain_map.update({
        f"entity:{i}:top": "Cluster 0"
        for i in range(METRICS_CACHE_CONCEPT_LIMIT + 25)
    })
    entity_concept_map = {
        eid: {"concept_id": "c0", "label": "Concept 0"}
        for eid in node_domain_map
    }

    metrics = CorpusMetrics(
        corpus_id="corpus",
        corpus_change_signature="sig",
        computed_at=datetime.utcnow(),
        node_count=len(node_domain_map),
        edge_count=100,
        density=0.1,
        cross_domain_edge_pct=0.0,
        modularity_proxy=1.0,
        domain_density={"Cluster 0": 0.1},
        per_domain_edge_counts={"Cluster 0": {"internal": 100, "external": 0}},
        relation_family_counts={"Structural": 100},
        top_pagerank=[],
        top_cross_domain_pagerank=[],
        node_domain_map=node_domain_map,
        node_domains_touched={eid: ["Cluster 0"] for eid in node_domain_map},
        frontier_candidates=[],
        fragile_bridges=[{"source": "entity:0:0", "target": "entity:1:0"}],
        terminological_gaps=[],
        structural_analogies=[],
        transfer_candidates=[],
        entity_name_map={eid: eid for eid in node_domain_map},
        concept_communities=concept_communities,
        entity_concept_map=entity_concept_map,
        entity_facet_map={eid: {"domain_type": "Concept"} for eid in node_domain_map},
        ontology_version="test",
        entity_betweenness={eid: 0.0 for eid in node_domain_map},
    )

    doc = _serialize_metrics(metrics)

    assert doc["cache_mode"] == "compact"
    assert len(doc["concept_communities"]) == METRICS_CACHE_CONCEPT_LIMIT
    assert len(doc["node_domain_map"]) <= METRICS_CACHE_MAP_ENTITY_LIMIT
    assert len(doc["entity_concept_map"]) <= METRICS_CACHE_MAP_ENTITY_LIMIT
    assert len(doc["entity_facet_map"]) <= METRICS_CACHE_MAP_ENTITY_LIMIT
    assert len(doc["concept_communities"][0]["member_ids"]) == 120
    assert doc["concept_communities"][0]["primary_domain"] == "Cluster 0"

    hydrated = _deserialize_metrics(doc)
    assert hydrated.concept_communities[0]["primary_domain"] == "Cluster 0"
    assert hydrated.node_count == len(node_domain_map)

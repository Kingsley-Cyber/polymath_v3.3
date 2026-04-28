"""Cached graph overview builder for the WebGL graph canvas.

The raw entity graph can grow far beyond what a browser should render. This
module turns the cached document-domain and concept-community analytics into a
small supernode graph: domains, concept neighborhoods, and aggregate detector
signals. It is intentionally cache-only; no Louvain/PageRank/Neo4j traversal
runs on the interactive path.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from services.graph.analytics import (
    DomainMap,
    get_cached_domain_map,
    get_cached_metrics,
)


async def get_cached_graph_overview(
    db,
    corpus_id: str,
    *,
    max_concepts: int = 80,
    max_edges: int = 220,
) -> dict[str, Any]:
    """Return a cached supernode graph for the corpus canvas.

    This is the million-scale entry point: the browser sees tens of overview
    nodes instead of every entity. If cache data is missing or stale, return a
    well-formed cache_warming payload so callers can avoid falling back to an
    expensive full-graph load.
    """
    domain_map = await get_cached_domain_map(db, corpus_id)
    if domain_map is None:
        return _cache_warming(corpus_id, "Graph analytics cache is warming.")

    metrics = await get_cached_metrics(
        db, corpus_id, domain_map.corpus_change_signature
    )
    if metrics is None:
        return _cache_warming(corpus_id, "Graph metrics cache is warming.")

    return build_overview_graph(
        domain_map,
        metrics,
        max_concepts=max_concepts,
        max_edges=max_edges,
    )


def build_overview_graph(
    domain_map: DomainMap,
    metrics,
    *,
    max_concepts: int = 80,
    max_edges: int = 220,
) -> dict[str, Any]:
    """Build a browser-sized graph from cached domains + concept communities."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    edge_scores: dict[str, float] = {}
    domain_node_by_name: dict[str, str] = {}

    def _add_edge(
        source: str,
        target: str,
        predicate: str,
        *,
        confidence: float = 1.0,
        weight: float = 1.0,
    ) -> None:
        if not source or not target or source == target:
            return
        key = f"{source}->{target}"
        edge_scores[key] = edge_scores.get(key, 0.0) + weight
        for edge in edges:
            if edge["source"] == source and edge["target"] == target:
                edge["confidence"] = max(float(edge.get("confidence") or 0.0), confidence)
                edge["weight"] = round(edge_scores[key], 3)
                return
        edges.append({
            "source": source,
            "target": target,
            "predicate": predicate,
            "confidence": confidence,
            "weight": round(weight, 3),
        })

    for cluster in sorted(domain_map.clusters.values(), key=lambda c: c.size, reverse=True):
        node_id = f"domain:{cluster.cluster_id}"
        domain_node_by_name[cluster.name] = node_id
        nodes.append({
            "id": node_id,
            "display_name": cluster.name,
            "entity_type": "domain",
            "mention_count": int(cluster.size),
            "supernode_type": "domain",
            "canonical_family": "",
            "object_kind": "",
            "domain_type": "",
            "ontology_version": metrics.ontology_version,
            "top_entities": cluster.top_entities[:6],
        })

    concepts = sorted(
        metrics.concept_communities or [],
        key=lambda c: (
            float(c.get("pagerank_sum") or 0.0),
            int(c.get("bridge_count") or 0),
            int(c.get("size") or 0),
        ),
        reverse=True,
    )[:max_concepts]
    concept_ids = {str(c.get("concept_id")) for c in concepts}
    concept_node_ids = {f"concept:{cid}" for cid in concept_ids if cid}

    for concept in concepts:
        concept_id = str(concept.get("concept_id") or "")
        if not concept_id:
            continue
        members = list(concept.get("member_ids") or [])
        domains = Counter(
            metrics.node_domain_map.get(eid, "unknown")
            for eid in members
            if metrics.node_domain_map.get(eid)
        )
        primary_domain = domains.most_common(1)[0][0] if domains else "unknown"
        node_id = f"concept:{concept_id}"
        nodes.append({
            "id": node_id,
            "display_name": str(concept.get("label") or "Concept Neighborhood"),
            "entity_type": "concept_community",
            "mention_count": int(concept.get("size") or 0),
            "supernode_type": "concept",
            "canonical_family": "",
            "object_kind": "",
            "domain_type": "",
            "ontology_version": metrics.ontology_version,
            "primary_domain": primary_domain,
            "top_entities": list(concept.get("top_entities") or [])[:6],
            "bridge_count": int(concept.get("bridge_count") or 0),
        })
        domain_node = domain_node_by_name.get(primary_domain)
        if domain_node:
            confidence = (
                domains[primary_domain] / max(1, sum(domains.values()))
                if domains else 1.0
            )
            _add_edge(domain_node, node_id, "contains", confidence=confidence, weight=confidence)

    def _concept_node_for_entity(entity_id: str) -> str:
        concept = metrics.entity_concept_map.get(entity_id) or {}
        concept_id = str(concept.get("concept_id") or "")
        node_id = f"concept:{concept_id}"
        return node_id if node_id in concept_node_ids else ""

    for bridge in (metrics.fragile_bridges or [])[: max_edges * 2]:
        source = _concept_node_for_entity(str(bridge.get("source") or ""))
        target = _concept_node_for_entity(str(bridge.get("target") or ""))
        _add_edge(source, target, "fragile_bridge", confidence=0.85, weight=2.0)

    for analogy in (metrics.structural_analogies or [])[: max_edges * 2]:
        source = _concept_node_for_entity(str(analogy.get("source") or ""))
        target = _concept_node_for_entity(str(analogy.get("target") or ""))
        sim = float(analogy.get("topology_sim") or 0.75)
        _add_edge(source, target, "structural_analog", confidence=sim, weight=1.2)

    for gap in (metrics.terminological_gaps or [])[: max_edges]:
        source = _concept_node_for_entity(str(gap.get("source") or ""))
        target = _concept_node_for_entity(str(gap.get("target") or ""))
        _add_edge(source, target, "terminological_gap", confidence=0.6, weight=0.6)

    domain_names = set(domain_node_by_name)
    for item in (metrics.top_cross_domain_pagerank or [])[: max_edges]:
        touched = [d for d in item.get("domains_touched", []) if d in domain_names]
        for idx, source_domain in enumerate(touched):
            for target_domain in touched[idx + 1:]:
                _add_edge(
                    domain_node_by_name[source_domain],
                    domain_node_by_name[target_domain],
                    "shared_hub",
                    confidence=0.7,
                    weight=0.8,
                )

    ranked_edges = sorted(
        edges,
        key=lambda e: (float(e.get("weight") or 0.0), float(e.get("confidence") or 0.0)),
        reverse=True,
    )
    truncated = len(ranked_edges) > max_edges
    return {
        "view": "overview",
        "status": "ready",
        "nodes": nodes,
        "edges": ranked_edges[:max_edges],
        "truncated": truncated,
        "raw_node_count": metrics.node_count,
        "raw_edge_count": metrics.edge_count,
        "concept_count": len(metrics.concept_communities or []),
        "domain_count": len(domain_map.clusters),
    }


def _cache_warming(corpus_id: str, message: str) -> dict[str, Any]:
    return {
        "view": "overview",
        "status": "cache_warming",
        "message": message,
        "nodes": [],
        "edges": [],
        "truncated": False,
        "raw_node_count": 0,
        "raw_edge_count": 0,
        "concept_count": 0,
        "domain_count": 0,
        "corpus_id": corpus_id,
    }

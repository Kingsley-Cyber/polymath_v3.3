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


def _dominant_facet(
    member_ids: list[str],
    facet_map: dict[str, dict[str, Any]],
    facet_key: str,
    *,
    top_k_distribution: int = 5,
) -> tuple[str, dict[str, int]]:
    """Pt 10a (Cluster 6) — aggregate the dominant ontology facet across cluster members.

    Supernodes (domain clusters, concept communities) represent many entities. The
    pre-Pt-10a overview emitted `canonical_family / object_kind / domain_type = ""`
    on every supernode because picking ONE entity's value would be a category error.
    This helper counts the facet values across the member sample and returns:
      - the most-common non-empty value (or "" if every member lacks the facet),
      - the top-N distribution for diagnostics / hover panels.

    For domain clusters the input is `cluster.top_entities` (top representative
    sample, since DomainCluster doesn't persist the full member list). For
    concept communities the input is `concept["member_ids"]` (full population).
    """
    counter = Counter(
        (facet_map.get(eid) or {}).get(facet_key)
        for eid in member_ids
        if facet_map.get(eid)
    )
    counter = Counter({k: v for k, v in counter.items() if k})
    if not counter:
        return "", {}
    dominant = counter.most_common(1)[0][0]
    distribution = dict(counter.most_common(top_k_distribution))
    return str(dominant), distribution


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
    facet_map = getattr(metrics, "entity_facet_map", None) or {}

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
        # Pt 10a — aggregate dominants over cluster.top_entities (DomainCluster
        # doesn't persist the full member list, so the top-K representatives
        # define the cluster's ontology character — accurate enough for color/shape).
        dom_family, family_dist = _dominant_facet(cluster.top_entities, facet_map, "canonical_family")
        dom_kind, kind_dist = _dominant_facet(cluster.top_entities, facet_map, "object_kind")
        dom_domain, domain_dist = _dominant_facet(cluster.top_entities, facet_map, "domain_type")
        nodes.append({
            "id": node_id,
            "display_name": cluster.name,
            "entity_type": "domain",
            "mention_count": int(cluster.size),
            "supernode_type": "domain",
            "canonical_family": dom_family,
            "object_kind": dom_kind,
            "domain_type": dom_domain,
            "family_distribution": family_dist,
            "kind_distribution": kind_dist,
            "domain_type_distribution": domain_dist,
            "ontology_version": metrics.ontology_version,
            # PR 2 multi-corpus rollout: cap bumped 6 → 50 so the frontend can
            # use this list as a client-side drill workaround when the new
            # POST /api/graph/cluster/{concept_id} endpoint is unavailable.
            # See GRAPH_VIEWER_BRIDGE.md §2.4 — interim until full drill ships.
            "top_entities": cluster.top_entities[:50],
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
        primary_domain = (
            domains.most_common(1)[0][0]
            if domains
            else str(concept.get("primary_domain") or "unknown")
        )
        node_id = f"concept:{concept_id}"
        # Pt 10a — aggregate dominants over the FULL concept-community member set
        # (more accurate than the domain-cluster path which only has top_entities).
        c_family, c_family_dist = _dominant_facet(members, facet_map, "canonical_family")
        c_kind, c_kind_dist = _dominant_facet(members, facet_map, "object_kind")
        c_domain, c_domain_dist = _dominant_facet(members, facet_map, "domain_type")
        nodes.append({
            "id": node_id,
            "display_name": str(concept.get("label") or "Concept Neighborhood"),
            "entity_type": "concept_community",
            "mention_count": int(concept.get("size") or 0),
            "supernode_type": "concept",
            "canonical_family": c_family,
            "object_kind": c_kind,
            "domain_type": c_domain,
            "family_distribution": c_family_dist,
            "kind_distribution": c_kind_dist,
            "domain_type_distribution": c_domain_dist,
            "ontology_version": metrics.ontology_version,
            "primary_domain": primary_domain,
            # PR 2: cap bumped 6 → 50, see comment on the domain branch above.
            "top_entities": list(concept.get("top_entities") or [])[:50],
            "member_ids": list(concept.get("member_ids") or [])[:200],
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


async def get_cached_graph_overview_multi(
    db,
    corpus_ids: list[str],
    *,
    max_concepts: int = 80,
    max_edges: int = 220,
) -> dict[str, Any]:
    """PR 2 — multi-corpus version of get_cached_graph_overview.

    Loads each corpus's cached overview independently and merges:
      • Nodes deduped by id; source_corpora tracks every corpus that surfaced
        the node.
      • Edges deduped by (source, target, predicate); source_corpora similar.
      • Per-corpus cache misses surface as `_meta.cache_warming_corpora`
        without failing the whole call.

    The merger never re-runs Louvain or any analytics — it composes already-
    cached supernode graphs. If a single corpus's cache is missing, that
    corpus contributes nothing but the rest still render. This is the
    "partial render + warming chip" behavior locked in
    GRAPH_VIEWER_BRIDGE.md §2.8.
    """
    if not corpus_ids:
        return {
            "view": "overview",
            "status": "ready",
            "nodes": [],
            "edges": [],
            "truncated": False,
            "raw_node_count": 0,
            "raw_edge_count": 0,
            "concept_count": 0,
            "domain_count": 0,
            "_meta": {
                "successful_ids": [],
                "failed_ids": [],
                "errors": {},
                "cache_warming_corpora": [],
            },
        }

    # Fan out per-corpus overview loads in parallel under a small concurrency
    # cap (no need to overwhelm Mongo with 32 simultaneous reads).
    import asyncio

    sem = asyncio.Semaphore(4)

    async def _load_one(cid: str) -> tuple[str, dict[str, Any] | None, str | None]:
        async with sem:
            try:
                payload = await get_cached_graph_overview(
                    db, cid, max_concepts=max_concepts, max_edges=max_edges
                )
                return cid, payload, None
            except Exception as exc:  # pragma: no cover — defensive
                return cid, None, str(exc)

    results = await asyncio.gather(*[_load_one(cid) for cid in corpus_ids])

    cache_warming: list[str] = []
    successful: list[str] = []
    errors: dict[str, str] = {}
    per_corpus: list[tuple[str, dict[str, Any]]] = []

    for cid, payload, err in results:
        if err is not None:
            errors[cid] = err
            continue
        if not payload:
            cache_warming.append(cid)
            continue
        if payload.get("status") == "cache_warming":
            cache_warming.append(cid)
            continue
        successful.append(cid)
        per_corpus.append((cid, payload))

    if not per_corpus:
        # All corpora cold — return cache_warming envelope so the UI shows
        # the chip and skips render.
        return {
            "view": "overview",
            "status": "cache_warming",
            "message": "All selected corpora are still warming.",
            "nodes": [],
            "edges": [],
            "truncated": False,
            "raw_node_count": 0,
            "raw_edge_count": 0,
            "concept_count": 0,
            "domain_count": 0,
            "_meta": {
                "successful_ids": successful,
                "failed_ids": list(errors.keys()),
                "errors": errors,
                "cache_warming_corpora": cache_warming,
            },
        }

    # Merge nodes by id; source_corpora accumulates every corpus that
    # surfaced the node. mention_count sums (so a concept seen in two
    # corpora visually scales). For string fields we keep the first
    # value seen — the corpora are sorted into per_corpus iteration
    # order for determinism (caller passes corpus_ids in a stable order
    # already; we rely on that).
    merged_nodes: dict[str, dict[str, Any]] = {}
    for cid, payload in per_corpus:
        for n in payload.get("nodes") or []:
            key = str(n.get("id") or "")
            if not key:
                continue
            existing = merged_nodes.get(key)
            if existing is None:
                merged = dict(n)
                merged["source_corpora"] = [cid]
                merged["source_corpus"] = cid
                merged_nodes[key] = merged
            else:
                if cid not in (existing.get("source_corpora") or []):
                    existing.setdefault("source_corpora", []).append(cid)
                # Aggregate size proxy across corpora.
                try:
                    existing["mention_count"] = int(existing.get("mention_count") or 0) + int(
                        n.get("mention_count") or 0
                    )
                except Exception:
                    pass

    merged_edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cid, payload in per_corpus:
        for e in payload.get("edges") or []:
            src = str(e.get("source") or "")
            tgt = str(e.get("target") or "")
            pred = str(e.get("predicate") or "")
            if not src or not tgt:
                continue
            key = (src, tgt, pred)
            existing = merged_edges.get(key)
            if existing is None:
                merged = dict(e)
                merged["source_corpora"] = [cid]
                merged["source_corpus"] = cid
                merged["dangling"] = (src not in merged_nodes) or (tgt not in merged_nodes)
                merged_edges[key] = merged
            else:
                if cid not in (existing.get("source_corpora") or []):
                    existing.setdefault("source_corpora", []).append(cid)
                # Confidence/weight reinforce: take max.
                try:
                    existing["confidence"] = max(
                        float(existing.get("confidence") or 0.0),
                        float(e.get("confidence") or 0.0),
                    )
                    existing["weight"] = max(
                        float(existing.get("weight") or 0.0),
                        float(e.get("weight") or 0.0),
                    )
                except Exception:
                    pass

    nodes = list(merged_nodes.values())
    edges = list(merged_edges.values())

    # Re-sort edges by combined weight so cap remains meaningful.
    edges.sort(
        key=lambda e: (float(e.get("weight") or 0.0), float(e.get("confidence") or 0.0)),
        reverse=True,
    )
    truncated = len(edges) > max_edges
    edges = edges[:max_edges]

    raw_node_total = sum(
        int(p.get("raw_node_count") or 0) for _, p in per_corpus
    )
    raw_edge_total = sum(
        int(p.get("raw_edge_count") or 0) for _, p in per_corpus
    )
    concept_total = sum(int(p.get("concept_count") or 0) for _, p in per_corpus)
    domain_total = sum(int(p.get("domain_count") or 0) for _, p in per_corpus)

    return {
        "view": "overview",
        "status": "ready",
        "nodes": nodes,
        "edges": edges,
        "truncated": truncated,
        "raw_node_count": raw_node_total,
        "raw_edge_count": raw_edge_total,
        "concept_count": concept_total,
        "domain_count": domain_total,
        "_meta": {
            "successful_ids": successful,
            "failed_ids": list(errors.keys()),
            "errors": errors,
            "cache_warming_corpora": cache_warming,
        },
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

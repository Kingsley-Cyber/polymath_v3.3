"""
Discourse graph — Phase 17 Wave 2.

Computes a term co-occurrence graph on-the-fly from MongoDB chunks for a
corpus, then runs structural analytics on it:

  * compute_discourse_graph  — tokenize chunks, build co-occurrence matrix,
                               return nodes + weighted edges
  * find_discourse_clusters  — community detection (networkx greedy modularity)
  * find_discourse_bridges   — words with high betweenness centrality AND
                               neighbors spanning multiple clusters
  * find_discourse_gaps      — cluster pairs with no bridging words
                               (DISCONNECTED) or <3 (THIN)
  * analyze_discourse_shape  — Gini of degree distribution + dominant cluster

No Neo4j writes. No GDS dependency. Uses the existing `(corpus_id, chunk_id)`
compound index on the `chunks` collection for corpus-scoped fetch.

Pipeline entrypoint:
    result = await build_discourse(db, corpus_id, top_terms=80, min_cooccur=3)
    -> {graph, clusters, bridges, gaps, shape}
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from itertools import combinations
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)

# Minimal stop word list. Not exhaustive — just high-noise words that would
# otherwise dominate the co-occurrence graph. Keep in sync with graph_query.py
# if the two modules ever diverge.
_STOP_WORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "if", "then", "else", "for",
        "of", "in", "on", "at", "to", "from", "by", "with", "as", "is",
        "are", "was", "were", "be", "been", "being", "have", "has", "had",
        "do", "does", "did", "will", "would", "could", "should", "may",
        "might", "must", "can", "this", "that", "these", "those", "i",
        "you", "he", "she", "it", "we", "they", "them", "their", "his",
        "her", "its", "our", "your", "my", "me", "us", "who", "whom",
        "what", "which", "when", "where", "why", "how", "all", "any",
        "both", "each", "few", "more", "most", "other", "some", "such",
        "no", "nor", "not", "only", "own", "same", "so", "than", "too",
        "very", "just", "also", "about", "after", "before", "between",
        "into", "through", "during", "above", "below", "under", "over",
        "up", "down", "out", "off", "there", "here", "again", "once",
        "will", "shall", "should", "ought", "need", "dare",
    }
)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'\-]{2,}")


# ── Tokenization ───────────────────────────────────────────────────────────


def extract_terms(text: str) -> list[str]:
    """
    Lowercase, regex-tokenize, drop stop words + single chars.
    Returns the raw token list for a chunk (duplicates preserved — caller
    decides whether to count multiplicity or set-ify).
    """
    return [
        t.lower()
        for t in _TOKEN_RE.findall(text or "")
        if t.lower() not in _STOP_WORDS and len(t) > 2
    ]


# ── Main graph builder ─────────────────────────────────────────────────────


async def compute_discourse_graph(
    db: Any,
    corpus_id: str,
    top_terms: int = 80,
    min_cooccur: int = 3,
    chunk_limit: int = 2000,
) -> dict:
    """
    Build a corpus-level discourse graph from MongoDB chunks.

    Steps:
      1. Fetch up to `chunk_limit` child chunks for the corpus
      2. Tokenize each → token frequency counter
      3. Keep the top `top_terms` by corpus-wide frequency as nodes
      4. Build co-occurrence counts over chunk-level pairs of kept terms
      5. Filter edges by `min_cooccur` threshold
      6. Return {nodes, links, term_freq} shape ready for rendering

    Runs in <5s for ~2k chunks on typical hardware (Python Counter + regex).
    """
    cursor = db["chunks"].find(
        {"corpus_id": corpus_id},
        projection={"text": 1, "_id": 0},
    ).limit(chunk_limit)

    chunk_texts: list[str] = []
    async for doc in cursor:
        t = doc.get("text") or ""
        if t.strip():
            chunk_texts.append(t)

    if not chunk_texts:
        logger.info("compute_discourse_graph: no chunks for corpus_id=%s", corpus_id)
        return {"nodes": [], "links": [], "term_freq": {}, "chunk_count": 0}

    # Step 2–3 — global term frequency, keep top-N
    global_counter: Counter[str] = Counter()
    per_chunk_sets: list[set[str]] = []
    for text in chunk_texts:
        tokens = extract_terms(text)
        if not tokens:
            per_chunk_sets.append(set())
            continue
        global_counter.update(tokens)
        # Set rather than multiset — co-occurrence uses chunk presence, not
        # within-chunk multiplicity.
        per_chunk_sets.append(set(tokens))

    keep_terms = {term for term, _ in global_counter.most_common(top_terms)}

    # Step 4 — co-occurrence counts over kept terms only
    cooccur: Counter[tuple[str, str]] = Counter()
    for token_set in per_chunk_sets:
        filtered = sorted(token_set & keep_terms)
        if len(filtered) < 2:
            continue
        for a, b in combinations(filtered, 2):
            cooccur[(a, b)] += 1

    # Step 5 — filter by min_cooccur
    links = [
        {"source": a, "target": b, "weight": weight}
        for (a, b), weight in cooccur.items()
        if weight >= min_cooccur
    ]

    # Emit only nodes that actually appear in at least one retained edge —
    # prevents the render from being cluttered with isolated high-frequency
    # terms that never co-occur.
    connected: set[str] = set()
    for link in links:
        connected.add(link["source"])
        connected.add(link["target"])

    nodes = [
        {
            "id": term,
            "label": term,
            "freq": global_counter[term],
            "type": "lexeme",
        }
        for term in sorted(connected)
    ]

    return {
        "nodes": nodes,
        "links": links,
        "term_freq": {t: global_counter[t] for t in connected},
        "chunk_count": len(chunk_texts),
    }


# ── networkx helpers ───────────────────────────────────────────────────────


def _to_nx(nodes: list[dict], links: list[dict]) -> nx.Graph:
    """Build a networkx.Graph from the discourse {nodes, links} payload."""
    g = nx.Graph()
    for n in nodes:
        g.add_node(n["id"], **{k: v for k, v in n.items() if k != "id"})
    for link in links:
        g.add_edge(link["source"], link["target"], weight=link.get("weight", 1))
    return g


# ── Clusters (community detection) ─────────────────────────────────────────


def find_discourse_clusters(nodes: list[dict], links: list[dict]) -> dict[str, int]:
    """
    Assign each node a cluster id via networkx greedy modularity.

    Returns: {term: cluster_id}. Empty dict if the graph is empty.
    No external dependency beyond networkx (no python-louvain needed).
    """
    if not nodes or not links:
        return {}

    g = _to_nx(nodes, links)
    try:
        from networkx.algorithms.community import greedy_modularity_communities

        communities = list(greedy_modularity_communities(g, weight="weight"))
    except Exception as exc:
        logger.warning(
            "greedy_modularity_communities failed (%s) — falling back to connected components",
            exc,
        )
        communities = [set(c) for c in nx.connected_components(g)]

    term_cluster: dict[str, int] = {}
    for cid, members in enumerate(communities):
        for term in members:
            term_cluster[term] = cid
    return term_cluster


# ── Bridges ────────────────────────────────────────────────────────────────


def find_discourse_bridges(
    nodes: list[dict],
    links: list[dict],
    term_cluster: dict[str, int],
    top_n: int = 10,
) -> list[dict]:
    """
    A word is a "discourse bridge" when it has high betweenness centrality AND
    its neighbors span ≥2 clusters. Those are the words that tie themes
    together — the semantic glue of the corpus.

    Returns: [{term, centrality, connects_clusters, degree}] top-N.
    """
    if not nodes or not links or not term_cluster:
        return []

    g = _to_nx(nodes, links)
    if g.number_of_nodes() == 0:
        return []

    # Betweenness centrality — normalized 0..1. For small graphs (<500 nodes)
    # this is fast; we're bounded by top_terms=80 by default so it's trivial.
    centrality: dict[str, float] = nx.betweenness_centrality(g, weight="weight")

    results: list[dict] = []
    for term, cent in centrality.items():
        if cent <= 0:
            continue
        neighbor_clusters = {
            term_cluster.get(nb) for nb in g.neighbors(term)
        }
        neighbor_clusters.discard(None)
        if len(neighbor_clusters) >= 2:
            results.append(
                {
                    "term": term,
                    "centrality": round(cent, 4),
                    "connects_clusters": sorted(neighbor_clusters),
                    "degree": g.degree(term),
                }
            )

    results.sort(key=lambda r: r["centrality"], reverse=True)
    return results[:top_n]


# ── Gaps ───────────────────────────────────────────────────────────────────


def find_discourse_gaps(
    nodes: list[dict],
    links: list[dict],
    term_cluster: dict[str, int],
) -> list[dict]:
    """
    For each pair of clusters, count the number of words that have neighbors
    in BOTH clusters. That count is the bridging density.

      * 0 bridging words   → DISCONNECTED (structural hole)
      * 1-2 bridging words → THIN (fragile connection)
      * 3+ bridging words  → healthy (not reported)

    Returns only DISCONNECTED + THIN pairs. Only meaningful when there are ≥2
    clusters.
    """
    if not term_cluster:
        return []

    g = _to_nx(nodes, links)
    cluster_ids = sorted(set(term_cluster.values()))
    if len(cluster_ids) < 2:
        return []

    # For each cluster, the set of terms in it
    cluster_members: dict[int, set[str]] = defaultdict(set)
    for term, cid in term_cluster.items():
        cluster_members[cid].add(term)

    # For each node, which clusters its neighbors belong to (set).
    node_neighbor_clusters: dict[str, set[int]] = {}
    for node in g.nodes:
        node_neighbor_clusters[node] = {
            term_cluster.get(nb) for nb in g.neighbors(node) if nb in term_cluster
        }

    gaps: list[dict] = []
    for ca, cb in combinations(cluster_ids, 2):
        # Bridging words: any term whose neighbor-cluster set contains BOTH ca and cb
        bridging_words = [
            term
            for term, nbc in node_neighbor_clusters.items()
            if ca in nbc and cb in nbc
        ]
        count = len(bridging_words)
        if count == 0:
            severity = "DISCONNECTED"
            interpretation = (
                "No word links these two discourse themes. A structural hole "
                "— themes coexist but never cross-reference."
            )
        elif count < 3:
            severity = "THIN"
            interpretation = (
                f"Only {count} word(s) bridge these themes. Fragile connection "
                "— a minor rewording could break the link."
            )
        else:
            continue  # healthy, don't report

        gaps.append(
            {
                "cluster_a": ca,
                "cluster_b": cb,
                "bridging_words": bridging_words[:5],  # cap for display
                "bridging_count": count,
                "severity": severity,
                "interpretation": interpretation,
            }
        )

    return gaps


# ── Shape ──────────────────────────────────────────────────────────────────


def _gini(values: list[float]) -> float:
    """
    Gini coefficient of a list of values (0 = equal, 1 = concentrated).
    Pure Python, O(N log N).
    """
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    total = sum(sorted_vals)
    if total == 0:
        return 0.0
    cumulative = 0.0
    for i, v in enumerate(sorted_vals, start=1):
        cumulative += i * v
    return (2 * cumulative) / (n * total) - (n + 1) / n


def analyze_discourse_shape(
    nodes: list[dict],
    links: list[dict],
    term_cluster: dict[str, int],
) -> dict:
    """
    Classify the shape of the discourse:

      CONCENTRATED — Gini > 0.6 (few high-degree hubs dominate)
      SKEWED      — one cluster holds > 50% of all edges
      DISPERSED   — >5 clusters, no single one > 25%
      BALANCED    — otherwise

    Also reports Gini, per-cluster edge proportions, and top-degree words.
    """
    if not nodes or not links:
        return {
            "shape": "EMPTY",
            "shape_description": "No discourse graph could be built.",
            "gini_coefficient": 0.0,
            "cluster_proportions": {},
            "dominant_cluster": None,
            "dominant_percentage": 0.0,
            "top_words_by_degree": [],
        }

    g = _to_nx(nodes, links)

    # Degree distribution Gini
    degrees = [float(g.degree(n)) for n in g.nodes]
    gini = _gini(degrees)

    # Edge share per cluster
    cluster_edge_count: dict[int, int] = defaultdict(int)
    total_edges = g.number_of_edges()
    for u, v in g.edges:
        cu = term_cluster.get(u)
        cv = term_cluster.get(v)
        # Count an intra-cluster edge in that cluster; inter-cluster edge in both
        if cu is not None:
            cluster_edge_count[cu] += 1
        if cv is not None and cv != cu:
            cluster_edge_count[cv] += 1

    cluster_proportions: dict[int, float] = {
        cid: round(count / max(1, total_edges), 3)
        for cid, count in cluster_edge_count.items()
    }

    if cluster_proportions:
        dominant_cluster, dominant_pct = max(
            cluster_proportions.items(), key=lambda kv: kv[1]
        )
    else:
        dominant_cluster, dominant_pct = None, 0.0

    # Top-degree words
    top_degrees = sorted(g.degree, key=lambda kv: kv[1], reverse=True)[:10]
    top_words_by_degree = [
        {"term": term, "degree": deg} for term, deg in top_degrees
    ]

    # Classify shape
    n_clusters = len({c for c in term_cluster.values()}) if term_cluster else 0
    if gini > 0.6:
        shape = "CONCENTRATED"
        desc = "A few hub words dominate the vocabulary — the corpus has a strong central vocabulary."
    elif dominant_pct > 0.5:
        shape = "SKEWED"
        desc = (
            f"One theme (cluster {dominant_cluster}) holds {dominant_pct:.0%} of edges — "
            "the discourse is weighted toward a single topic."
        )
    elif n_clusters > 5 and dominant_pct < 0.25:
        shape = "DISPERSED"
        desc = (
            f"{n_clusters} distinct vocabulary clusters with no dominant theme — "
            "the corpus covers many subjects without central focus."
        )
    else:
        shape = "BALANCED"
        desc = "Vocabulary themes are distributed roughly evenly with healthy bridging."

    return {
        "shape": shape,
        "shape_description": desc,
        "gini_coefficient": round(gini, 3),
        "cluster_proportions": cluster_proportions,
        "dominant_cluster": dominant_cluster,
        "dominant_percentage": round(dominant_pct, 3),
        "top_words_by_degree": top_words_by_degree,
    }


# ── Pipeline entrypoint ────────────────────────────────────────────────────


async def build_discourse(
    db: Any,
    corpus_id: str,
    top_terms: int = 80,
    min_cooccur: int = 3,
    chunk_limit: int = 2000,
) -> dict:
    """
    One-shot pipeline that the router calls. Returns everything the frontend
    needs in a single payload.
    """
    graph = await compute_discourse_graph(
        db=db,
        corpus_id=corpus_id,
        top_terms=top_terms,
        min_cooccur=min_cooccur,
        chunk_limit=chunk_limit,
    )
    nodes = graph["nodes"]
    links = graph["links"]

    term_cluster = find_discourse_clusters(nodes, links)

    # Attach cluster id to each node for frontend coloring
    for n in nodes:
        n["cluster"] = term_cluster.get(n["id"])

    clusters = _summarize_clusters(term_cluster, graph["term_freq"])
    bridges = find_discourse_bridges(nodes, links, term_cluster)
    gaps = find_discourse_gaps(nodes, links, term_cluster)
    shape = analyze_discourse_shape(nodes, links, term_cluster)

    return {
        "graph": {"nodes": nodes, "links": links},
        "chunk_count": graph["chunk_count"],
        "clusters": clusters,
        "bridges": bridges,
        "gaps": gaps,
        "shape": shape,
    }


def _summarize_clusters(
    term_cluster: dict[str, int],
    term_freq: dict[str, int],
) -> list[dict]:
    """
    Per-cluster summary: id, size, top-5 terms by frequency.
    """
    if not term_cluster:
        return []

    by_cluster: dict[int, list[str]] = defaultdict(list)
    for term, cid in term_cluster.items():
        by_cluster[cid].append(term)

    summaries: list[dict] = []
    for cid, members in sorted(by_cluster.items()):
        top_terms = sorted(members, key=lambda t: term_freq.get(t, 0), reverse=True)[:5]
        summaries.append(
            {
                "cluster_id": cid,
                "size": len(members),
                "top_terms": top_terms,
            }
        )
    return summaries

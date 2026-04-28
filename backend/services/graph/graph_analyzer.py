"""
Graph analyzer — Phase 17 Wave 3.

LLM-powered structural synthesis for the three Agent Query modes. Core
invariant (from PHASE_17_GRAPH_DISCOVERY §Critical Invariants):

    The LLM reads STRUCTURE, not TEXT. Every prompt is built from
    pre-computed prose sections describing topology (hubs, bridges, gaps,
    articulation points, alignment scores). No raw chunks are ever shown
    to the LLM in this module.

Three public synthesis calls (one per mode):
  - synthesize_knowledge(query, knowledge_nodes, knowledge_links, seed_ids)
  - synthesize_discourse(discourse_nodes, discourse_links, clusters, bridges, gaps, shape)
  - synthesize_split(knowledge, discourse, overlay)

Plus one pure-Python helper for the Split canvas (no LLM, deterministic):
  - compute_split_overlay(knowledge_nodes, knowledge_links, discourse_nodes, discourse_links)

All synthesizers return `{markdown: str, handoff_prompt: str, structural_summary: dict}`.
The handoff_prompt is the prose the frontend injects into the chat when the
user clicks "→ Ask Chat" on a finding.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)


# ── Knowledge structural summary ────────────────────────────────────────────


def compute_knowledge_summary(
    nodes: list[dict],
    links: list[dict],
    seed_ids: list[str] | None = None,
) -> dict:
    """
    Pre-compute the structural features the knowledge-mode LLM prompt needs.
    All extraction is purely topological — never reads node text.
    """
    seed_set = set(seed_ids or [])
    if not nodes:
        return {
            "node_count": 0,
            "link_count": 0,
            "seed_count": 0,
            "density": 0.0,
            "top_hubs": [],
            "bridges": [],
            "articulation_points": [],
            "components": 0,
            "largest_component_size": 0,
        }

    g = nx.Graph()
    for n in nodes:
        g.add_node(n["id"], **{k: v for k, v in n.items() if k != "id"})
    for link in links:
        s = link["source"]
        t = link["target"]
        if isinstance(s, dict):
            s = s.get("id", s)
        if isinstance(t, dict):
            t = t.get("id", t)
        g.add_edge(s, t, **{k: v for k, v in link.items() if k not in ("source", "target")})

    # Top hubs by degree
    top_hubs_raw = sorted(g.degree, key=lambda kv: kv[1], reverse=True)[:5]
    top_hubs = [
        {
            "id": nid,
            "name": g.nodes[nid].get("display_name", nid),
            "degree": deg,
            "is_seed": nid in seed_set,
        }
        for nid, deg in top_hubs_raw
    ]

    # Bridges: nodes touching ≥2 seeds via neighbors
    bridges: list[dict] = []
    if seed_set:
        for nid in g.nodes:
            if nid in seed_set:
                continue
            connected_seeds = {nb for nb in g.neighbors(nid) if nb in seed_set}
            if len(connected_seeds) >= 2:
                bridges.append(
                    {
                        "id": nid,
                        "name": g.nodes[nid].get("display_name", nid),
                        "connected_seeds": sorted(connected_seeds),
                    }
                )
        bridges.sort(key=lambda b: -len(b["connected_seeds"]))
        bridges = bridges[:5]

    # Articulation points — nodes whose removal disconnects the graph.
    # networkx returns a generator of node ids.
    try:
        art_points = [
            {"id": nid, "name": g.nodes[nid].get("display_name", nid)}
            for nid in nx.articulation_points(g)
        ][:5]
    except Exception as exc:
        logger.warning("articulation_points failed: %s", exc)
        art_points = []

    components = list(nx.connected_components(g))
    largest = max((len(c) for c in components), default=0)

    possible_edges = max(1, g.number_of_nodes() * (g.number_of_nodes() - 1) / 2)
    density = round(g.number_of_edges() / possible_edges, 4)

    return {
        "node_count": g.number_of_nodes(),
        "link_count": g.number_of_edges(),
        "seed_count": len(seed_set & set(g.nodes)),
        "density": density,
        "top_hubs": top_hubs,
        "bridges": bridges,
        "articulation_points": art_points,
        "components": len(components),
        "largest_component_size": largest,
    }


# ── Discourse structural summary ────────────────────────────────────────────


def compute_discourse_summary(
    nodes: list[dict],
    links: list[dict],
    clusters: list[dict],
    bridges: list[dict],
    gaps: list[dict],
    shape: dict,
) -> dict:
    """
    Pre-compute discourse-side prose sections. Most of the work is already
    done by `services/discourse.py` — this just rolls it into the shape the
    synthesize step wants.
    """
    top_terms_by_degree: list[dict] = shape.get("top_words_by_degree") or []
    return {
        "node_count": len(nodes),
        "link_count": len(links),
        "cluster_count": len(clusters),
        "cluster_sizes": [{"cluster_id": c["cluster_id"], "size": c["size"]} for c in clusters],
        "top_clusters": [
            {"cluster_id": c["cluster_id"], "top_terms": c.get("top_terms") or []}
            for c in clusters[:5]
        ],
        "bridges": [
            {
                "term": b["term"],
                "centrality": b.get("centrality", 0.0),
                "spans": b.get("connects_clusters") or [],
            }
            for b in bridges[:5]
        ],
        "gaps": [
            {
                "severity": g["severity"],
                "between": [g["cluster_a"], g["cluster_b"]],
                "bridging_count": g["bridging_count"],
            }
            for g in gaps[:5]
        ],
        "top_terms": [t["term"] for t in top_terms_by_degree[:8]],
        "shape": shape.get("shape", "UNKNOWN"),
        "shape_description": shape.get("shape_description", ""),
        "gini": shape.get("gini_coefficient", 0.0),
    }


# ── Split overlay (pure Python, deterministic) ─────────────────────────────


def compute_split_overlay(
    knowledge_nodes: list[dict],
    knowledge_links: list[dict],
    discourse_nodes: list[dict],
    discourse_links: list[dict],
) -> dict:
    """
    Merge the two canvases into a single renderable graph + compute the
    alignment score (intersection / union).

    Cross-type edges are drawn when an entity's `display_name` (lowered,
    trimmed) equals a lexeme's `id` (which is already the lowercased term).
    Those edges are marked `type="crosslink"` so the frontend can style them
    distinctly (e.g. dashed violet).
    """
    # Normalize entity names once
    entity_by_normname: dict[str, dict] = {}
    for n in knowledge_nodes:
        name = (n.get("display_name") or n.get("name") or n.get("id") or "").strip().lower()
        if name:
            entity_by_normname.setdefault(name, n)

    lexeme_ids: set[str] = {n["id"] for n in discourse_nodes}

    # Intersection: entity-names that match a lexeme term
    intersection = sorted(entity_by_normname.keys() & lexeme_ids)
    union_size = len(entity_by_normname.keys() | lexeme_ids)
    alignment_score = (
        round(len(intersection) / union_size, 4) if union_size else 0.0
    )

    # Cross-type edges (entity_id ↔ lexeme_id)
    crosslinks: list[dict] = []
    for term in intersection:
        entity_node = entity_by_normname[term]
        crosslinks.append(
            {
                "source": entity_node["id"],
                "target": term,
                "predicate": "mentions_lexeme",
                "type": "crosslink",
                "confidence": 1.0,
            }
        )

    # Merge node sets — entities keep circle shape, lexemes keep square.
    # Tag every node with `mode` so the frontend knows which pane it came from.
    merged_nodes: list[dict] = []
    for n in knowledge_nodes:
        merged_nodes.append({**n, "mode": "knowledge", "isLexeme": False})
    # Dedup in case an entity already carries its normalized name as an id
    seen_ids = {n["id"] for n in merged_nodes}
    for n in discourse_nodes:
        if n["id"] in seen_ids:
            continue
        merged_nodes.append({**n, "mode": "discourse", "isLexeme": True})

    # Merge link sets — preserve originals, then append crosslinks
    merged_links: list[dict] = []
    for l in knowledge_links:
        merged_links.append({**l, "mode": "knowledge"})
    for l in discourse_links:
        merged_links.append({**l, "mode": "discourse"})
    merged_links.extend(crosslinks)

    return {
        "nodes": merged_nodes,
        "links": merged_links,
        "alignment": {
            "intersection": intersection,
            "intersection_size": len(intersection),
            "union_size": union_size,
            "score": alignment_score,
            "entities_present_as_lexemes": intersection,
            "entities_absent_from_lexemes": sorted(
                entity_by_normname.keys() - lexeme_ids
            )[:20],
        },
        "crosslinks_count": len(crosslinks),
    }


def compute_split_summary(
    knowledge_summary: dict,
    discourse_summary: dict,
    overlay: dict,
) -> dict:
    """
    Roll the two summaries + overlay alignment into one structural picture
    for the Split synthesizer prompt.
    """
    return {
        "knowledge": knowledge_summary,
        "discourse": discourse_summary,
        "alignment": overlay.get("alignment", {}),
        "crosslinks_count": overlay.get("crosslinks_count", 0),
    }


# ── LLM synthesis ──────────────────────────────────────────────────────────


_SYSTEM_PROMPT = (
    "You are a structural graph analyst. Given topological features of a "
    "graph (never raw document text), explain what the shape means. Be "
    "concrete, reference the specific hubs/bridges/gaps by name. Keep it "
    "under 250 words. Format as markdown with short paragraphs — no "
    "headings, no bullet lists unless the structure genuinely warrants it."
)


def _format_knowledge_prompt(query: str | None, summary: dict) -> str:
    hubs = ", ".join(
        f"{h['name']} (degree {h['degree']}{'*seed' if h['is_seed'] else ''})"
        for h in summary.get("top_hubs", [])
    ) or "none"
    bridges = (
        "; ".join(
            f"{b['name']} connects seeds {b['connected_seeds']}"
            for b in summary.get("bridges", [])
        )
        or "none"
    )
    articulation = ", ".join(a["name"] for a in summary.get("articulation_points", [])) or "none"
    sections = [
        f"Mode: Knowledge subgraph",
        f"Query: {query or '(no query — default subgraph)'}",
        f"Graph: {summary['node_count']} entities, {summary['link_count']} relations, {summary['components']} connected components (largest: {summary['largest_component_size']})",
        f"Density: {summary['density']}",
        f"Top hubs: {hubs}",
        f"Bridges to ≥2 seeds: {bridges}",
        f"Articulation points (single-point-of-failure entities): {articulation}",
    ]
    return (
        "Based on the STRUCTURAL features of a corpus entity graph described "
        "below, explain what the topology reveals about the corpus — what the "
        "central actors are, where the structural glue holds, and where there "
        "are fragile connections. Do not invent content; only describe the "
        "shape.\n\n" + "\n".join(sections)
    )


def _format_discourse_prompt(summary: dict) -> str:
    top_clusters = "; ".join(
        f"Cluster {c['cluster_id']} ({', '.join(c['top_terms'][:3])})"
        for c in summary.get("top_clusters", [])
    ) or "none"
    bridges = ", ".join(
        f"{b['term']} (cent {b['centrality']:.2f}, spans {b['spans']})"
        for b in summary.get("bridges", [])
    ) or "none"
    gaps = "; ".join(
        f"{g['severity']} between clusters {g['between']} ({g['bridging_count']} bridging words)"
        for g in summary.get("gaps", [])
    ) or "none"
    sections = [
        f"Mode: Discourse (vocabulary co-occurrence)",
        f"Graph: {summary['node_count']} lexemes, {summary['link_count']} co-occurrence edges, {summary['cluster_count']} clusters",
        f"Shape: {summary['shape']} — {summary['shape_description']}",
        f"Gini of degree distribution: {summary['gini']}",
        f"Top clusters: {top_clusters}",
        f"Cross-cluster bridging words: {bridges}",
        f"Gaps: {gaps}",
    ]
    return (
        "Based on the STRUCTURAL features of a corpus's vocabulary "
        "co-occurrence graph described below, explain what themes this corpus "
        "covers, how they relate, and where the discourse has structural "
        "holes (disconnected themes). Do not invent topics; only describe the "
        "shape revealed by the words that co-occur.\n\n" + "\n".join(sections)
    )


def _format_split_prompt(split_summary: dict) -> str:
    k = split_summary["knowledge"]
    d = split_summary["discourse"]
    alignment = split_summary["alignment"]
    aligned_pct = (alignment.get("score", 0.0) or 0.0) * 100
    hubs = ", ".join(h["name"] for h in k.get("top_hubs", [])[:3]) or "none"
    top_clusters = "; ".join(
        f"{', '.join(c['top_terms'][:3])}" for c in d.get("top_clusters", [])[:3]
    ) or "none"
    intersection = ", ".join(alignment.get("intersection", [])[:8]) or "none"
    absent = ", ".join(alignment.get("entities_absent_from_lexemes", [])[:8]) or "none"

    sections = [
        "Mode: Split (entity graph + discourse graph overlaid)",
        f"Entity graph: {k.get('node_count', 0)} nodes, {k.get('link_count', 0)} relations",
        f"Discourse graph: {d.get('node_count', 0)} lexemes, {d.get('link_count', 0)} co-occurrence edges, shape={d.get('shape', 'UNKNOWN')}",
        f"Entity hubs: {hubs}",
        f"Top discourse clusters: {top_clusters}",
        f"Alignment score (entity↔lexeme intersection / union): {aligned_pct:.1f}%",
        f"Entities that surface as lexemes: {intersection}",
        f"Entities that do NOT surface as lexemes: {absent}",
    ]
    return (
        "Based on the STRUCTURAL overlay of a corpus's entity graph and its "
        "vocabulary co-occurrence graph described below, explain what the "
        "ALIGNMENT reveals. High alignment (many entities surface as lexemes) "
        "means the corpus talks about its entities directly; low alignment "
        "means entities are mentioned tangentially or named differently from "
        "how people discuss them. Call out specific entities that are absent "
        "from the discourse vocabulary.\n\n" + "\n".join(sections)
    )


async def synthesize_knowledge(
    query: str | None,
    nodes: list[dict],
    links: list[dict],
    seed_ids: list[str] | None,
    model: str | None = None,
) -> dict:
    from services.llm import llm_service

    summary = compute_knowledge_summary(nodes, links, seed_ids)
    prompt = _format_knowledge_prompt(query, summary)
    narrative = await _llm_narrate(llm_service, prompt, model)
    handoff = _build_handoff(query, summary, mode="knowledge", narrative=narrative)
    return {
        "markdown": narrative,
        "structural_summary": summary,
        "handoff_prompt": handoff,
    }


async def synthesize_discourse(
    nodes: list[dict],
    links: list[dict],
    clusters: list[dict],
    bridges: list[dict],
    gaps: list[dict],
    shape: dict,
    model: str | None = None,
) -> dict:
    from services.llm import llm_service

    summary = compute_discourse_summary(nodes, links, clusters, bridges, gaps, shape)
    prompt = _format_discourse_prompt(summary)
    narrative = await _llm_narrate(llm_service, prompt, model)
    handoff = _build_handoff(None, summary, mode="discourse", narrative=narrative)
    return {
        "markdown": narrative,
        "structural_summary": summary,
        "handoff_prompt": handoff,
    }


async def synthesize_split(
    query: str | None,
    knowledge_nodes: list[dict],
    knowledge_links: list[dict],
    seed_ids: list[str] | None,
    discourse_nodes: list[dict],
    discourse_links: list[dict],
    clusters: list[dict],
    bridges: list[dict],
    gaps: list[dict],
    shape: dict,
    model: str | None = None,
) -> dict:
    from services.llm import llm_service

    k_summary = compute_knowledge_summary(knowledge_nodes, knowledge_links, seed_ids)
    d_summary = compute_discourse_summary(
        discourse_nodes, discourse_links, clusters, bridges, gaps, shape
    )
    overlay = compute_split_overlay(
        knowledge_nodes, knowledge_links, discourse_nodes, discourse_links
    )
    split_summary = compute_split_summary(k_summary, d_summary, overlay)
    prompt = _format_split_prompt(split_summary)
    narrative = await _llm_narrate(llm_service, prompt, model)
    handoff = _build_handoff(query, split_summary, mode="split", narrative=narrative)
    return {
        "markdown": narrative,
        "structural_summary": split_summary,
        "overlay": overlay,
        "handoff_prompt": handoff,
    }


# ── Internals ──────────────────────────────────────────────────────────────


async def _llm_narrate(llm_service, prompt: str, model: str | None) -> str:
    """
    Single-turn LLM call. On failure returns a deterministic fallback so the
    UI still shows something rather than a blank panel.
    """
    try:
        return (
            await llm_service.complete_sync(
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                model=model,
                temperature=0.3,
                max_tokens=800,
            )
        ).strip() or "_LLM returned empty output._"
    except Exception as exc:
        logger.warning("graph_analyzer LLM narrate failed: %s", exc)
        return (
            "_LLM narration unavailable. Showing structural summary only:_\n\n"
            + prompt
        )


def _build_handoff(
    query: str | None, summary: dict, mode: str, narrative: str
) -> str:
    """
    Build the chat message the frontend seeds when the user clicks
    `→ Ask Chat`. This gives the chat model the structural context so it
    can answer follow-up questions without having to recompute anything.
    """
    header = (
        f"**Context — Graph Analysis ({mode.upper()} mode)**\n"
        f"Based on the graph structure for this corpus:\n\n"
    )
    body = narrative.strip()
    footer = ""
    if query:
        footer = f"\n\nMy question: {query}"
    return f"{header}{body}{footer}"

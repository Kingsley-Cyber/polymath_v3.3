from __future__ import annotations

import importlib.machinery
import json
import logging
import re
import sys
import time as _time
import types
from pathlib import Path
from typing import Any, Optional

try:
    import bson as _bson  # noqa: F401
except ModuleNotFoundError:
    _bson_stub = types.ModuleType("bson")

    class ObjectId(str):
        @staticmethod
        def is_valid(_value: object) -> bool:
            return True

    _bson_stub.ObjectId = ObjectId
    sys.modules["bson"] = _bson_stub

_LEGACY_PYC = Path(__file__).with_name("_orchestrator_legacy.cpython-311.pyc")
logger = logging.getLogger(__name__)
_LEGACY_MISSING_MESSAGE = (
    "Graph discovery legacy scope module is unavailable. Reconstruct "
    "services.graph._orchestrator_legacy as tracked Python source, or restore "
    "_orchestrator_legacy.cpython-311.pyc for legacy graph discovery."
)

_legacy = None
if _LEGACY_PYC.exists():
    try:
        _legacy = importlib.machinery.SourcelessFileLoader(
            "_polymath_legacy_graph_orchestrator",
            str(_LEGACY_PYC),
        ).load_module()
    except Exception as exc:
        logger.warning(
            "Legacy graph orchestrator failed to load from %s: %s",
            _LEGACY_PYC,
            exc,
        )
else:
    logger.warning("%s Path=%s", _LEGACY_MISSING_MESSAGE, _LEGACY_PYC)

if _legacy is not None:
    for _name, _value in vars(_legacy).items():
        if not _name.startswith("__"):
            globals()[_name] = _value


async def _legacy_llm_synthesis_stub(
    messages: list[dict[str, str]],
    *,
    user_id: Optional[str] = None,
    model_override: Optional[str] = None,
    agentic: bool = False,
    max_tokens: int = 5000,
    temperature: float = 0.3,
    timeout: float = 120.0,
) -> str:
    """Keep legacy scoping but remove the old remote synthesis call.

    The public graph query path now makes exactly one user-visible synthesis
    call in this module. The preserved legacy orchestrator still builds bounded
    graph scope, evidence, and compatibility fields; this stub prevents its old
    template-style LLM read from adding latency or steering the narrative.
    """

    logger.info(
        "Skipping legacy graph synthesis LLM; auto-synthesis packet will make the single LLM call "
        "model=%s user_id=%s agentic=%s",
        model_override or "(selected/default)",
        user_id or "",
        agentic,
    )
    return json.dumps(
        {
            "interpretation": (
                "Legacy synthesis is disabled for Auto-Synthesis Mission Control; "
                "the curated GraphInsightPacket provides the narrative source of truth."
            ),
            "frontier": [],
            "analogies": [],
            "bridges": [],
            "weak_links": [],
            "transfers": [],
            "questions": [],
        }
    )


if _legacy is not None:
    _legacy._call_llm = _legacy_llm_synthesis_stub


def schedule_graph_discovery_cache_warm(*args: Any, **kwargs: Any) -> None:
    """Schedule legacy graph cache warm when the legacy scope module exists."""

    if _legacy is None:
        logger.debug("Skipping graph cache warm: %s", _LEGACY_MISSING_MESSAGE)
        return None
    warm = getattr(_legacy, "schedule_graph_discovery_cache_warm", None)
    if callable(warm):
        return warm(*args, **kwargs)
    logger.debug("Skipping graph cache warm: legacy warm function is missing")
    return None


# Hard caps for the LLM input packet. Graph Query should synthesize from a
# curated brief, not a retrieval dump. Keep normal input near ~1.5k tokens.
_PACKET_MAX_ENTITIES = 12
_PACKET_MAX_COMMUNITIES = 5
_PACKET_MAX_EDGES = 14
_PACKET_MAX_GAPS = 3
_PACKET_MAX_SIGNALS = 4
_PACKET_MAX_WEAK_LINKS = 3
_PACKET_MAX_EVIDENCE = 6
_PACKET_EVIDENCE_TEXT_LIMIT = 260
_PACKET_RETRIEVER_PRE_RERANK_K = 40
_PACKET_RETRIEVER_RERANK_POOL = 40
_PACKET_RETRIEVER_GRAPH_EXPANSION = 20
_PACKET_MAX_GATEWAYS = 5
_PACKET_MAX_SUPPORTING_STATEMENTS = 4

# Synthesis-time LLM budget. Keep the call fast; the packet is bounded so the
# model has plenty of room without burning the whole turn budget. Longer essay
# paragraphs (5-7 sentences each, ~3 themes + 2 bridges + 2 gaps + 2 signals)
# need ~3500 output tokens to land cleanly without truncation.
_SYNTHESIS_TIMEOUT_SECONDS = 120.0
_SYNTHESIS_MAX_TOKENS = 2200
_SYNTHESIS_TEMPERATURE = 0.45
_CONTEXT_MAX_GROUPS = 10
_CONTEXT_MAX_CONCEPT_NODES = 64
_CONTEXT_MAX_DOCUMENT_NODES = 8
_GRAPH_RELEVANCE_STOPWORDS = {
    "explore",
    "concept",
    "neighborhood",
    "around",
    "bridge",
    "bridges",
    "cross-domain",
    "domain",
    "corpus",
    "query",
    "its",
}


def _text(value: Any, limit: int = 700) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text[:limit]


def _ids(values: list[Any] | tuple[Any, ...]) -> list[str]:
    return [str(v) for v in values if v]


def _graph_relevance_terms(query: str) -> set[str]:
    return {
        term
        for term in _query_terms_for_evidence(query)
        if term not in _GRAPH_RELEVANCE_STOPWORDS
    }


def _entity_relevance_text(
    raw: dict[str, Any],
    entity_concept_map: dict[str, Any],
) -> str:
    eid = str(raw.get("id") or raw.get("entity_id") or "")
    concept = entity_concept_map.get(eid, {}) or {}
    parts = [
        raw.get("label"),
        raw.get("name"),
        raw.get("concept"),
        raw.get("domain"),
        raw.get("domain_type"),
        raw.get("object_kind"),
        raw.get("canonical_family"),
        concept.get("label"),
        concept.get("concept_id"),
    ]
    return " ".join(str(part).lower() for part in parts if part)


def _query_relevant_entity_ids(
    graph_nodes: list[dict[str, Any]],
    selected_edges: list[dict[str, Any]],
    *,
    query: str,
    entity_concept_map: dict[str, Any],
) -> set[str]:
    """Keep the packet focused on query terms plus direct graph neighbors.

    The legacy scope can intentionally over-recall. The synthesis packet cannot:
    if an unrelated concept community sneaks in, the LLM will treat it as an
    invitation to synthesize. This filter keeps direct one-hop structure around
    query-matching concepts while dropping unrelated corpus neighborhoods.
    """

    node_ids = {str(raw.get("id") or "").strip() for raw in graph_nodes if raw.get("id")}
    terms = _graph_relevance_terms(query)
    if not node_ids or not terms:
        return node_ids

    core_ids = {
        str(raw.get("id") or "").strip()
        for raw in graph_nodes
        if raw.get("id")
        and any(term in _entity_relevance_text(raw, entity_concept_map) for term in terms)
    }
    if not core_ids:
        return node_ids

    relevant = set(core_ids)
    for edge in selected_edges:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or edge.get("source_entity_id") or "").strip()
        target = str(edge.get("target") or edge.get("target_entity_id") or "").strip()
        if source in core_ids and target in node_ids:
            relevant.add(target)
        if target in core_ids and source in node_ids:
            relevant.add(source)
    return relevant


def _auto_item(
    title: str,
    body: str,
    evidence: list[str] | None = None,
    related_ids: list[str] | None = None,
) -> dict[str, Any]:
    title = re.sub(r"\s*/\s*", " ", _text(title, 120))
    body = re.sub(r"\b[Tt]rends?\b", "signals", _text(body, 900))
    body = re.sub(r"\b[Tt]rending\b", "emerging", body)
    return {
        "title": title,
        "body": body,
        "evidence": evidence or ["graph packet"],
        "related_ids": related_ids or [],
    }


def _auto_synthesis_from_result(result: Any) -> dict[str, Any]:
    headline_payload = getattr(result, "headline", {}) or {}
    headline = (
        headline_payload.get("headline")
        if isinstance(headline_payload, dict)
        else ""
    ) or getattr(result, "interpretation", "") or "Mission Control synthesis"

    themes = [
        _auto_item(
            t.get("name") or f"Theme {idx}",
            " ".join(t.get("prose") or []) or f"{t.get('name', 'This theme')} is active in the query scope.",
            [t.get("theme_id") or f"theme:{idx}"],
            [t.get("theme_id") or ""],
        )
        for idx, t in enumerate(getattr(result, "themes", []) or [], start=1)
    ][:6]

    bridges = [
        _auto_item(
            b.get("subhead") or f"Bridge {idx}",
            " ".join(b.get("prose") or []) or "Cached bridge evidence connects these graph neighborhoods.",
            [b.get("bridge_id") or f"bridge:{idx}"],
            _ids([b.get("source_entity_id"), b.get("target_entity_id")]),
        )
        for idx, b in enumerate(getattr(result, "bridges_v2", []) or [], start=1)
    ][:5]

    gaps = [
        _auto_item(
            g.get("question") or f"Gap {idx}",
            " ".join(g.get("prose") or []) or "This is a thin expected connection, not a proven missing edge.",
            [g.get("gap_id") or f"gap:{idx}"],
            _ids([g.get("cluster_a"), g.get("cluster_b")]),
        )
        for idx, g in enumerate(getattr(result, "gaps_v2", []) or [], start=1)
    ][:5]

    signals = [
        _auto_item(
            s.get("canonical_name") or f"Emerging signal {idx}",
            " ".join(s.get("prose") or [])
            or (
                "Non-temporal signal: this concept acts as an integration point "
                "inside the scoped packet."
            ),
            [s.get("entity_id") or f"signal:{idx}"],
            [s.get("entity_id") or ""],
        )
        for idx, s in enumerate(getattr(result, "latent_topics", []) or [], start=1)
    ][:5]

    next_moves = [
        _auto_item(
            q.get("text") or f"Next move {idx}",
            q.get("text") or "Run a narrower follow-up query against this graph packet.",
            ["next_move"],
            [],
        )
        for idx, q in enumerate(getattr(result, "questions", []) or [], start=1)
    ][:5]
    if not next_moves:
        next_moves = [
            _auto_item(
                "Inspect the evidence trace",
                "Use the trace to verify which anchors, chunks, entities, and edges actually supported this synthesis.",
                ["trace"],
                [],
            )
        ]

    evidence_notes = [
        _auto_item(
            w.get("weakness_type", "Weak link").replace("_", " ").title(),
            w.get("rationale") or w.get("evidence") or "This is a provenance warning, not a gap.",
            [w.get("source") or "weak_link", w.get("target") or ""],
            _ids([w.get("source"), w.get("target")]),
        )
        for w in (getattr(result, "weak_links", []) or [])[:5]
    ]
    if not evidence_notes:
        evidence_notes = [
            _auto_item(
                "Evidence packet",
                "No weak-link warnings were generated for this bounded graph packet.",
                ["weak_links:0"],
                [],
            )
        ]

    return {
        "headline": _text(headline, 220),
        "themes": themes,
        "bridges": bridges,
        "gaps": gaps,
        "emerging_signals": signals,
        "next_moves": next_moves,
        "evidence_notes": evidence_notes,
    }


def _trace_with_stages(trace: dict[str, Any] | None) -> dict[str, Any]:
    trace = dict(trace or {})
    expansion = trace.get("graph_expansion") or {}
    if trace.get("stages"):
        return trace
    trace["stages"] = [
        {
            "stage": "seed",
            "label": "Seed",
            "count": int(expansion.get("seed_entities") or 0),
            "status": "ok" if expansion.get("seed_entities") else "watch",
            "detail": "Seed anchors from terms and vector neighbors.",
        },
        {
            "stage": "expanded",
            "label": "Expanded",
            "count": int(expansion.get("expanded_entities") or 0),
            "status": "ok" if expansion.get("expanded_entities") else "watch",
            "detail": "Concept/facet expanded graph scope.",
        },
        {
            "stage": "working_set",
            "label": "Working set",
            "count": int(expansion.get("working_entities") or len(trace.get("working_entities") or [])),
            "status": "ok" if trace.get("working_entities") else "watch",
            "detail": "Bounded entities selected for synthesis.",
        },
        {
            "stage": "selected_edges",
            "label": "Selected edges",
            "count": len(trace.get("selected_edges") or []),
            "status": "ok" if trace.get("selected_edges") else "watch",
            "detail": "Bounded Neo4j overlay edges.",
        },
        {
            "stage": "source_docs",
            "label": "Source docs",
            "count": len(trace.get("source_docs") or []),
            "status": "ok" if trace.get("source_docs") else "watch",
            "detail": "Source chunks used for packet evidence.",
        },
    ]
    return trace


def _insight_packet_summary_from_result(result: Any) -> dict[str, Any]:
    trace = getattr(result, "trace", {}) or {}
    counts = {
        "anchors": len(getattr(result, "anchors", []) or []),
        "working_entities": len((trace.get("working_entities") if isinstance(trace, dict) else []) or []),
        "themes": len(getattr(result, "themes", []) or []),
        "bridges": len(getattr(result, "bridges_v2", []) or []),
        "gaps": len(getattr(result, "gaps_v2", []) or []),
        "emerging_signals": len(getattr(result, "latent_topics", []) or []),
        "weak_links": len(getattr(result, "weak_links", []) or []),
        "evidence_chunks": len((trace.get("source_docs") if isinstance(trace, dict) else []) or []),
        "context_edges": len((trace.get("selected_edges") if isinstance(trace, dict) else []) or []),
    }
    temporal_support = any(
        bool(doc.get("created_at") or doc.get("updated_at") or doc.get("date"))
        for doc in ((trace.get("source_docs") if isinstance(trace, dict) else []) or [])
        if isinstance(doc, dict)
    )
    evidence_filter = trace.get("evidence_filter") if isinstance(trace, dict) else {}
    if not isinstance(evidence_filter, dict):
        evidence_filter = {}
    evidence_all_rejected = bool(evidence_filter.get("all_rejected"))
    sparse = (
        evidence_all_rejected
        or (
            counts["evidence_chunks"] == 0
            and counts["anchors"] == 0
            and counts["themes"] < 2
            and counts["bridges"] == 0
            and counts["gaps"] == 0
        )
    )
    return {
        "sparse": sparse,
        "temporal_support": temporal_support,
        "counts": counts,
        "evidence_sources": {
            "chunks": counts["evidence_chunks"],
            "cached_metrics": counts["themes"] + counts["bridges"] + counts["gaps"] + counts["emerging_signals"],
            "bounded_neo4j_edges": counts["context_edges"],
            "provenance_warnings": counts["weak_links"],
        },
        "fallback_reason": None,
    }


def _context_graph_from_result(result: Any) -> dict[str, Any]:
    """Build the visual map from the query working set, not corpus buckets.

    The old context map used the first cached concept communities as large
    islands. That made the view feel like the whole corpus had already been
    bucketed. This map treats those cached communities only as labels/facets
    for the nodes that actually appeared in the bounded query result. Source
    documents stay unique evidence nodes, so the user can see which files fed
    the synthesis instead of seeing a generic corpus geography.
    """

    graph = getattr(result, "graph", {}) or {"nodes": [], "links": []}
    trace = _coerce_dict(getattr(result, "trace", {}) or {})
    evidence_filter = trace.get("evidence_filter") if isinstance(trace.get("evidence_filter"), dict) else {}
    if evidence_filter.get("all_rejected"):
        return {
            "nodes": [],
            "links": [],
            "meta": {
                "default_view": "context_map",
                "topic_source": "withheld",
                "document_source": "bounded_llm_evidence_files",
                "overlay_source": "withheld_low_value_evidence",
                "grouping_basis": "Candidate chunks were rejected by the evidence-quality gate, so graph neighborhoods are hidden for this query.",
                "corpus_bucketed": False,
                "topic_count": 0,
                "document_count": 0,
                "concept_count": 0,
                "hidden_concept_count": 0,
                "visible_concept_cap": _CONTEXT_MAX_CONCEPT_NODES,
                "evidence_gate": "all_candidate_chunks_failed_quality_filter",
            },
        }
    entity_concept_map = getattr(result, "entity_concept_map", {}) or {}
    graph_nodes_raw = [raw for raw in (graph.get("nodes", []) or []) if isinstance(raw, dict)]
    selected_edges_raw = [
        edge for edge in (trace.get("selected_edges") or []) if isinstance(edge, dict)
    ]
    relevance_entity_ids = _query_relevant_entity_ids(
        graph_nodes_raw,
        selected_edges_raw,
        query=str(getattr(result, "query", "") or ""),
        entity_concept_map=entity_concept_map,
    )
    if relevance_entity_ids:
        graph_nodes_for_map = [
            raw
            for raw in graph_nodes_raw
            if str(raw.get("id") or "").strip() in relevance_entity_ids
        ]
    else:
        graph_nodes_for_map = graph_nodes_raw
    themes_by_id = {
        str(t.get("theme_id") or ""): t
        for t in (getattr(result, "themes", []) or [])
        if isinstance(t, dict)
    }
    bridge_entities = {
        str(v)
        for b in (getattr(result, "bridges_v2", []) or [])
        for v in (b.get("source_entity_id"), b.get("target_entity_id"))
        if v
    }
    gap_clusters = {
        str(v)
        for g in (getattr(result, "gaps_v2", []) or [])
        for v in (g.get("cluster_a"), g.get("cluster_b"))
        if v
    }
    weak_entities = {
        str(v)
        for w in (getattr(result, "weak_links", []) or [])
        for v in (w.get("source"), w.get("target"))
        if v
    }
    if relevance_entity_ids:
        bridge_entities &= relevance_entity_ids
        weak_entities &= relevance_entity_ids

    nodes: dict[str, dict[str, Any]] = {}
    links: list[dict[str, Any]] = []
    group_index: dict[str, dict[str, Any]] = {}
    entity_group: dict[str, str] = {}
    cluster_to_group: dict[str, str] = {}

    def _group_for_entity(eid: str, raw: dict[str, Any]) -> tuple[str, str, str | None]:
        concept = entity_concept_map.get(eid, {}) or {}
        cid = str(concept.get("concept_id") or "").strip()
        label = str(
            (themes_by_id.get(cid) or {}).get("name")
            or concept.get("label")
            or raw.get("concept")
            or raw.get("canonical_family")
            or raw.get("domain_type")
            or raw.get("domain")
            or raw.get("object_kind")
            or raw.get("label")
            or "Query neighborhood"
        ).strip()
        if cid:
            return f"concept:{cid}", label, cid
        key_seed = label.lower().replace(" ", "-")[:80] or eid
        return f"facet:{key_seed}", label, None

    # Query-scoped concept neighborhoods: only entities present in the bounded
    # working graph can create a visible island.
    for raw in graph_nodes_for_map:
        eid = str(raw.get("id") or "").strip()
        if not eid:
            continue
        group_id, group_label, raw_cluster_id = _group_for_entity(eid, raw)
        entity_group[eid] = group_id
        if raw_cluster_id:
            cluster_to_group[raw_cluster_id] = group_id
        group = group_index.setdefault(
            group_id,
            {
                "id": group_id,
                "label": group_label,
                "entities": [],
                "degree": 0.0,
                "bridge_count": 0,
                "cluster_id": raw_cluster_id,
            },
        )
        label = str(raw.get("label") or eid)
        if label not in group["entities"]:
            group["entities"].append(label)
        group["degree"] += float(raw.get("degree") or 0)
        if str(raw.get("emphasis") or "") in {"bridge", "bridge_anchor", "frontier", "transfer_hub"} or eid in bridge_entities:
            group["bridge_count"] += 1

    sorted_groups = sorted(
        group_index.values(),
        key=lambda g: (len(g["entities"]), float(g.get("degree") or 0), int(g.get("bridge_count") or 0)),
        reverse=True,
    )[:_CONTEXT_MAX_GROUPS]
    visible_group_ids = {str(group["id"]) for group in sorted_groups}

    required_entity_ids = set(bridge_entities) | set(weak_entities)
    for raw in graph.get("links", []) or []:
        if not isinstance(raw, dict):
            continue
        source_id = str(raw.get("source") or "")
        target_id = str(raw.get("target") or "")
        if relevance_entity_ids and (
            source_id not in relevance_entity_ids or target_id not in relevance_entity_ids
        ):
            continue
        emphasis = str(raw.get("emphasis") or "")
        if "bridge" in emphasis or emphasis in {"gap_edge", "weak_edge", "fragile_bridge", "ghost_analogy"}:
            if raw.get("source"):
                required_entity_ids.add(str(raw.get("source")))
            if raw.get("target"):
                required_entity_ids.add(str(raw.get("target")))

    def _node_rank(raw: dict[str, Any]) -> tuple[float, float]:
        eid = str(raw.get("id") or "")
        role = str(raw.get("emphasis") or "")
        role_score = 0.0
        if eid in required_entity_ids:
            role_score += 100.0
        if role in {"transfer_hub", "bridge_anchor", "bridge"}:
            role_score += 60.0
        elif role in {"frontier", "analogy_anchor", "analogy"}:
            role_score += 35.0
        return (role_score + float(raw.get("degree") or 0), float(raw.get("degree") or 0))

    visible_entity_ids = {
        str(raw.get("id"))
        for raw in sorted(graph_nodes_for_map, key=_node_rank, reverse=True)[:_CONTEXT_MAX_CONCEPT_NODES]
        if raw.get("id")
    }
    visible_entity_ids |= {
        eid for eid in required_entity_ids if any(str(raw.get("id") or "") == eid for raw in graph_nodes_for_map)
    }
    hidden_concept_count = max(0, len(graph_nodes_for_map) - len(visible_entity_ids))

    for group in sorted_groups:
        gid = str(group["id"])
        cluster_id = str(group.get("cluster_id") or gid)
        nodes[f"topic:{gid}"] = {
            "id": f"topic:{gid}",
            "label": str(group.get("label") or "Query neighborhood"),
            "kind": "topic",
            "role": "query_concept_neighborhood",
            "topic_id": gid,
            "size": max(8.0, float(len(group["entities"])) * 2.0 + float(group.get("bridge_count") or 0)),
            "weight": float(group.get("degree") or len(group["entities"])),
            "evidence_count": len(group["entities"]),
            "top_entities": [str(v) for v in group["entities"][:8]],
            "jump_targets": [{"section": "themes", "label": "theme", "detail": str(group.get("label") or "Query neighborhood"), "target_id": cluster_id}],
        }

    for raw in graph_nodes_for_map:
        eid = str(raw.get("id") or "").strip()
        if not eid or eid not in visible_entity_ids:
            continue
        group_id = entity_group.get(eid)
        concept = entity_concept_map.get(eid, {}) or {}
        raw_cluster_id = str(concept.get("concept_id") or "").strip()
        jumps = []
        if group_id:
            jumps.append({"section": "themes", "label": "neighborhood", "detail": str(concept.get("label") or raw.get("concept") or raw.get("domain") or "Query neighborhood"), "target_id": raw_cluster_id or group_id})
        if eid in bridge_entities:
            jumps.append({"section": "bridges", "label": "bridge", "detail": "Bridge evidence involving this concept", "target_id": eid})
        if raw_cluster_id in gap_clusters or (group_id in visible_group_ids and raw_cluster_id in gap_clusters):
            jumps.append({"section": "gaps", "label": "gap", "detail": "Gap candidate involving this query neighborhood", "target_id": raw_cluster_id or group_id})
        if eid in weak_entities:
            jumps.append({"section": "trace", "label": "weak link", "detail": "Provenance warning involving this concept", "target_id": eid})
        if not jumps:
            jumps.append({"section": "trace", "label": "trace", "detail": "Working-set trace for this selected graph", "target_id": eid})
        nodes[eid] = {
            "id": eid,
            "label": raw.get("label") or eid,
            "kind": "concept",
            "role": raw.get("emphasis") or "query_entity",
            "topic_id": group_id if group_id in visible_group_ids else None,
            "size": 3.0 + min(10.0, float(raw.get("degree") or 0) ** 0.5),
            "weight": float(raw.get("degree") or 0),
            "evidence_count": 1,
            "top_entities": [],
            "jump_targets": jumps,
        }
        topic_id = f"topic:{group_id}" if group_id else ""
        if topic_id in nodes:
            links.append({
                "source": topic_id,
                "target": eid,
                "kind": "membership",
                "role": "query_neighborhood",
                "weight": 0.45,
                "suggested": False,
                "evidence": "entity grouped by query-scoped concept/facet similarity",
            })

    # Unique source documents from the LLM evidence packet. They are not merged
    # into corpus buckets; the map shows each file that contributed chunks.
    files_by_id: dict[str, dict[str, Any]] = {}
    for raw in trace.get("source_docs") or []:
        if not isinstance(raw, dict):
            continue
        doc_id = str(raw.get("doc_id") or raw.get("document_id") or raw.get("id") or "unknown-doc").strip()
        chunk_id = str(raw.get("chunk_id") or raw.get("id") or "").strip()
        source_label = _source_label_from_row(raw, doc_id=doc_id, chunk_id=chunk_id)
        row = files_by_id.setdefault(
            doc_id,
            {
                "doc_id": doc_id,
                "source_label": source_label,
                "chunk_count": 0,
                "chunk_ids": [],
                "has_temporal": False,
                "source": raw.get("source") if isinstance(raw.get("source"), dict) else {},
            },
        )
        row["chunk_count"] += 1
        if chunk_id:
            row["chunk_ids"].append(chunk_id)
        row["has_temporal"] = bool(row["has_temporal"] or _has_temporal_source_support(raw))
        if not row.get("source") and isinstance(raw.get("source"), dict):
            row["source"] = raw.get("source") or {}

    primary_groups = [str(group["id"]) for group in sorted_groups[:3]]
    for doc in sorted(files_by_id.values(), key=lambda d: (-int(d.get("chunk_count") or 0), str(d.get("source_label") or "")))[:_CONTEXT_MAX_DOCUMENT_NODES]:
        doc_node_id = f"doc:{doc['doc_id']}"
        primary_group = primary_groups[0] if primary_groups else None
        nodes[doc_node_id] = {
            "id": doc_node_id,
            "label": doc.get("source_label") or doc.get("doc_id"),
            "kind": "document",
            "role": "evidence_document",
            "topic_id": primary_group,
            "size": 5.0 + min(8.0, float(doc.get("chunk_count") or 1)),
            "weight": float(doc.get("chunk_count") or 1),
            "evidence_count": int(doc.get("chunk_count") or 0),
            "source": doc.get("source") or {},
            "top_entities": [str(v) for v in (doc.get("chunk_ids") or [])[:6]],
            "jump_targets": [{"section": "trace", "label": "file", "detail": str(doc.get("source_label") or doc.get("doc_id")), "target_id": str(doc.get("doc_id") or "")}],
        }
        for gid in primary_groups:
            topic_id = f"topic:{gid}"
            if topic_id in nodes:
                links.append({
                    "source": doc_node_id,
                    "target": topic_id,
                    "kind": "evidence_context",
                    "role": "document_context",
                    "weight": 0.25,
                    "suggested": False,
                    "evidence": "source file contributed chunks to the bounded LLM packet",
                })

    for raw in graph.get("links", []) or []:
        if not isinstance(raw, dict):
            continue
        if relevance_entity_ids and (
            str(raw.get("source") or "") not in relevance_entity_ids
            or str(raw.get("target") or "") not in relevance_entity_ids
        ):
            continue
        emphasis = str(raw.get("emphasis") or "context")
        if str(raw.get("source") or "") not in nodes or str(raw.get("target") or "") not in nodes:
            continue
        links.append({
            "source": raw.get("source"),
            "target": raw.get("target"),
            "kind": raw.get("classification") or "context",
            "role": emphasis,
            "weight": 2.2 if emphasis == "bridge" else 1.3 if "bridge" in emphasis else 0.6,
            "suggested": emphasis == "gap_edge",
            "evidence": raw.get("evidence") or raw.get("predicate") or "",
        })

    for gap in getattr(result, "gaps_v2", []) or []:
        if not isinstance(gap, dict):
            continue
        a = cluster_to_group.get(str(gap.get("cluster_a") or ""))
        b = cluster_to_group.get(str(gap.get("cluster_b") or ""))
        if not a or not b or a == b:
            continue
        source = f"topic:{a}"
        target = f"topic:{b}"
        if source in nodes and target in nodes:
            links.append({
                "source": source,
                "target": target,
                "kind": "suggested_gap",
                "role": "gap_suggestion",
                "weight": 0.2,
                "suggested": True,
                "evidence": gap.get("question") or "suggested gap, not a Neo4j edge",
            })

    return {
        "nodes": list(nodes.values()),
        "links": links,
        "meta": {
            "default_view": "context_map",
            "topic_source": "query_scoped_concept_neighborhoods",
            "document_source": "bounded_llm_evidence_files",
            "overlay_source": "bounded_neo4j_query",
            "grouping_basis": "Only concepts/documents surfaced by this query are grouped; cached corpus communities are not used as default islands.",
            "corpus_bucketed": False,
            "topic_count": len([n for n in nodes.values() if n.get("kind") == "topic"]),
            "document_count": len([n for n in nodes.values() if n.get("kind") == "document"]),
            "concept_count": len([n for n in nodes.values() if n.get("kind") == "concept"]),
            "hidden_concept_count": hidden_concept_count,
            "visible_concept_cap": _CONTEXT_MAX_CONCEPT_NODES,
        },
    }


def _coerce_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _qdrant_collections_for_packet(corpus_id: str) -> dict[str, str]:
    try:
        from services.storage.qdrant_writer import _col_for_corpus

        return {
            kind: _col_for_corpus(corpus_id, kind)
            for kind in ("naive", "hrag", "graph", "schemas")
        }
    except Exception:
        prefix = corpus_id[:8]
        return {
            "naive": f"corpus_{prefix}_naive",
            "hrag": f"corpus_{prefix}_hrag",
            "graph": f"corpus_{prefix}_graph",
            "schemas": f"corpus_{prefix}_schemas",
        }


def _parent_id_from_summary_chunk(chunk_id: str) -> str:
    if chunk_id.endswith("_summary"):
        return chunk_id[: -len("_summary")]
    return chunk_id


_LOW_VALUE_EVIDENCE_RE = re.compile(
    r"\b("
    r"bibliography|references|works cited|index|table of contents|contents|"
    r"copyright|all rights reserved|electronic copy available|ssrn|isbn|doi|"
    r"journal of|proceedings|retrieved from|available at"
    r")\b",
    re.IGNORECASE,
)
_LOW_VALUE_EVIDENCE_FLAGS = {"low_value_section", "index_like", "front_matter_like"}
_INDEX_ROW_RE = re.compile(r"\b[A-Za-z][A-Za-z -]{2,},\s*\d{1,4}\b")


def _query_terms_for_evidence(query: str) -> set[str]:
    short_domain_terms = {"ai", "ml", "ui", "ux", "db", "kg"}
    stopwords = {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "how",
        "what",
        "why",
        "into",
        "onto",
        "about",
        "between",
        "patterns",
        "pattern",
        "concept",
        "concepts",
        "neighborhood",
        "neighborhoods",
        "bridge",
        "bridges",
        "cross",
        "domain",
        "corpus",
        "query",
        "explore",
    }
    terms = {
        term.lower()
        for term in re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,}", query or "")
    }
    return {
        term
        for term in terms
        if term
        and (len(term) >= 3 or term in short_domain_terms)
        and term not in stopwords
    }


def _terms_from_values(*values: Any) -> set[str]:
    return _query_terms_for_evidence(" ".join(str(value or "") for value in values))


def _evidence_term_index(rows: list[dict[str, Any]]) -> set[str]:
    terms: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        terms.update(
            _terms_from_values(
                row.get("text"),
                row.get("source_label"),
                source.get("title"),
                source.get("filename"),
                source.get("section"),
            )
        )
    return terms


def _edge_term_index(edges: list[dict[str, Any]]) -> set[str]:
    terms: set[str] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        terms.update(
            _terms_from_values(
                edge.get("source_name"),
                edge.get("target_name"),
                edge.get("predicate"),
                edge.get("relation_family"),
            )
        )
    return terms


def _gap_cluster_terms(gap: dict[str, Any], side: str) -> set[str]:
    label = gap.get(f"cluster_{side}_label") or gap.get(f"cluster_{side}") or ""
    terms = _terms_from_values(label)
    terms.update(_terms_from_values(gap.get("question")))
    anchors = gap.get("anchor_concepts") or []
    if isinstance(anchors, list):
        terms.update(_terms_from_values(*anchors))
    coherence = gap.get("coherence") if isinstance(gap.get("coherence"), dict) else {}
    for key in ("shared_terms", "shared_families", "shared_domain_types", "shared_neighbors"):
        values = coherence.get(key)
        if isinstance(values, list):
            terms.update(_terms_from_values(*values))
    return terms


def _gap_supported_by_scope(
    gap: dict[str, Any],
    *,
    relevant_cluster_ids: set[str],
    query_terms: set[str],
    evidence_terms: set[str],
    edge_terms: set[str],
) -> tuple[bool, str]:
    """Gate speculative gaps so unrelated corpus regions do not become synthesis bait."""

    if not relevant_cluster_ids:
        return False, "no_query_cluster"

    cluster_a = str(gap.get("cluster_a") or "")
    cluster_b = str(gap.get("cluster_b") or "")
    a_relevant = cluster_a in relevant_cluster_ids
    b_relevant = cluster_b in relevant_cluster_ids
    if a_relevant and b_relevant:
        return True, "both_query_clusters"
    if not (a_relevant or b_relevant):
        return False, "outside_query_scope"

    support_terms = (evidence_terms | edge_terms) - query_terms
    off_scope_side = "b" if a_relevant else "a"
    off_scope_terms = _gap_cluster_terms(gap, off_scope_side) - query_terms
    if off_scope_terms and support_terms and off_scope_terms & support_terms:
        return True, "off_scope_terms_supported_by_evidence"
    return False, "off_scope_cluster_not_in_evidence"


def _evidence_quality(raw: dict[str, Any], query_terms: set[str]) -> tuple[float, list[str]]:
    text = _text(raw.get("text") or raw.get("chunk_text") or "", 1400)
    lowered = text.lower()
    reasons: list[str] = []
    score = float(raw.get("score") or 0.0) * 2.0

    term_hits = sum(1 for term in query_terms if term in lowered)
    score += min(4.0, term_hits * 0.9)
    if term_hits:
        reasons.append(f"query_terms:{term_hits}")
    else:
        score -= 1.5
        reasons.append("no_query_terms")

    sentence_count = len(re.findall(r"[.!?](?:\s|$)", text))
    if sentence_count >= 2:
        score += 1.2
    elif len(text) > 180:
        score += 0.4

    if len(text) < 120:
        score -= 1.0
        reasons.append("short")

    low_value_hits = len(_LOW_VALUE_EVIDENCE_RE.findall(lowered))
    indexish_hits = len(_INDEX_ROW_RE.findall(text))
    if low_value_hits:
        score -= 4.0 + low_value_hits
        reasons.append("low_value_section")
    if indexish_hits >= 4:
        score -= 4.0
        reasons.append("index_like")
    heading_text = " ".join(str(v) for v in (raw.get("heading_path") or []))
    if "page_1" in heading_text and term_hits < 2 and "abstract" not in lowered:
        score -= 2.5
        reasons.append("front_matter_like")

    # Summary chunks are useful only when their text is actually topical.
    chunk_id = str(raw.get("chunk_id") or raw.get("id") or "")
    if chunk_id.endswith("_summary") and term_hits:
        score += 0.5

    return score, reasons


def _first_metadata_text(*values: Any, limit: int = 140) -> str:
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            parts = [str(v).strip() for v in value if str(v).strip()]
            if parts:
                return _text(", ".join(parts[:3]), limit)
            continue
        if isinstance(value, dict):
            continue
        text = _text(value, limit)
        if text:
            return text
    return ""


def _basename_metadata_text(value: Any, limit: int = 160) -> str:
    text = _first_metadata_text(value, limit=limit)
    if not text:
        return ""
    return _text(text.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1], limit)


def _looks_like_internal_doc_label(value: Any, *ids: Any) -> bool:
    label = _first_metadata_text(value, limit=240)
    if not label:
        return True
    normalized_ids = {
        _first_metadata_text(v, limit=240)
        for v in ids
        if _first_metadata_text(v, limit=240)
    }
    if label in normalized_ids:
        return True
    if label.startswith("doc:") and label[4:] in normalized_ids:
        return True
    bare = label[4:] if label.startswith("doc:") else label
    return bool(re.fullmatch(r"[0-9a-fA-F]{8,}", bare))


def _metadata_value(metadata: dict[str, Any], *keys: str, limit: int = 140) -> str:
    for key in keys:
        if key in metadata:
            value = _first_metadata_text(metadata.get(key), limit=limit)
            if value:
                return value
    return ""


def _merged_document_metadata(doc: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key in (
        "metadata",
        "document_metadata",
        "source_metadata",
        "ebook_metadata",
        "pdf_metadata",
    ):
        value = doc.get(key)
        if isinstance(value, dict):
            merged.update(value)
    for key in (
        "title",
        "author",
        "authors",
        "publisher",
        "publication_date",
        "published_at",
        "date_published",
        "genre",
        "description",
    ):
        if key in doc:
            merged.setdefault(key, doc.get(key))
    return merged


def _source_label_from_row(
    row: dict[str, Any],
    *,
    doc: dict[str, Any] | None = None,
    doc_id: str = "",
    chunk_id: str = "",
) -> str:
    """Choose a human source label, treating doc-id/hash labels as internal."""

    doc = doc or {}
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    source_meta = row.get("source_meta") if isinstance(row.get("source_meta"), dict) else {}
    identity_values = (
        doc_id,
        row.get("doc_id"),
        row.get("document_id"),
        row.get("id"),
        chunk_id,
    )
    existing = _first_metadata_text(
        row.get("source_label"),
        row.get("doc_title"),
        row.get("filename"),
        limit=160,
    )
    if existing and not _looks_like_internal_doc_label(existing, *identity_values):
        return existing

    metadata = _merged_document_metadata(doc) if doc else {}
    candidates = [
        doc.get("filename"),
        source.get("filename"),
        source_meta.get("filename"),
        row.get("filename"),
        _basename_metadata_text(doc.get("source_path")),
        _basename_metadata_text(source.get("source_path")),
        _basename_metadata_text(source_meta.get("source_path")),
        doc.get("title"),
        _metadata_value(metadata, "title", "dc:title", limit=160) if metadata else "",
        source.get("title"),
        source_meta.get("title"),
        row.get("doc_title"),
        existing,
    ]
    for candidate in candidates:
        label = _first_metadata_text(candidate, limit=160)
        if label and not _looks_like_internal_doc_label(label, *identity_values):
            return label
    return existing or _first_metadata_text(doc_id, chunk_id, "unknown-doc", limit=160)


def _source_type(filename: str, source_mime: str) -> str:
    mime = source_mime.lower().strip()
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    if "pdf" in mime or suffix == "pdf":
        return "pdf"
    if "html" in mime or suffix in {"html", "htm"}:
        return "html"
    if "markdown" in mime or suffix in {"md", "markdown"}:
        return "markdown"
    if "epub" in mime or suffix == "epub":
        return "ebook"
    if "word" in mime or suffix in {"doc", "docx"}:
        return "document"
    if "text" in mime or suffix in {"txt", "log"}:
        return "text"
    return suffix or (mime.split("/")[-1] if "/" in mime else mime)


def _heading_label(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return _text(" > ".join(str(v).strip() for v in value if str(v).strip()), 120)
    return _text(value, 120)


def _page_range(*values: Any) -> str:
    start = None
    end = None
    for value in values:
        if not isinstance(value, dict):
            continue
        start = start if start is not None else value.get("page_start")
        end = end if end is not None else value.get("page_end")
    if start is None and end is None:
        return ""
    if end is None or str(start) == str(end):
        return str(start or end)
    return f"{start}-{end}"


def _schema_hints(schema_lens: Any) -> dict[str, Any]:
    if not isinstance(schema_lens, dict):
        return {}
    hints = {
        "domains": [str(v) for v in (schema_lens.get("corpus_domains") or [])[:2] if v],
        "object_kinds": [str(v) for v in (schema_lens.get("object_kinds") or [])[:2] if v],
        "relations": [str(v) for v in (schema_lens.get("preferred_relations") or [])[:3] if v],
    }
    return {key: value for key, value in hints.items() if value}


def _compact_source_meta(
    *,
    doc: dict[str, Any],
    parent: dict[str, Any],
    chunk: dict[str, Any],
    row: dict[str, Any],
    source_label: str,
    heading_path: Any,
) -> dict[str, Any]:
    metadata = _merged_document_metadata(doc)
    filename = _first_metadata_text(doc.get("filename"), row.get("filename"), limit=160)
    source_mime = _first_metadata_text(doc.get("source_mime"), row.get("source_mime"), limit=80)
    title = _metadata_value(metadata, "title", "dc:title", limit=160)

    source: dict[str, Any] = {
        "title": title or source_label,
        "filename": filename,
        "source_type": _source_type(filename or source_label, source_mime),
        "section": _heading_label(heading_path),
        "page_range": _page_range(row, chunk, parent),
        "source_tier": _first_metadata_text(
            chunk.get("source_tier"),
            parent.get("source_tier"),
            row.get("source_tier"),
            doc.get("source_tier"),
            limit=60,
        ),
        "author": _metadata_value(metadata, "author", "authors", "creator", "dc:creator"),
        "publisher": _metadata_value(metadata, "publisher", "dc:publisher"),
        "publication_date": _metadata_value(
            metadata,
            "publication_date",
            "published_at",
            "date_published",
            "publish_date",
            "dc:date",
        ),
        "genre": _metadata_value(metadata, "genre", "category", "subject"),
        "description": _metadata_value(metadata, "description", "summary", "abstract", limit=220),
        "hints": _schema_hints(doc.get("schema_lens")),
    }
    if source_mime:
        source["mime"] = source_mime
    return {key: value for key, value in source.items() if value not in ("", [], {}, None)}


def _has_temporal_source_support(row: dict[str, Any]) -> bool:
    source = row.get("source") if isinstance(row.get("source"), dict) else {}
    source_meta = row.get("source_meta") if isinstance(row.get("source_meta"), dict) else {}
    return bool(
        row.get("publication_date")
        or row.get("published_at")
        or row.get("date_published")
        or row.get("source_date")
        or row.get("document_date")
        or row.get("event_date")
        or row.get("date")
        or source.get("publication_date")
        or source_meta.get("publication_date")
    )


def _curated_evidence_rows(
    source_docs: list[Any],
    *,
    query: str,
) -> tuple[list[dict[str, Any]], int, bool, dict[str, int]]:
    query_terms = _query_terms_for_evidence(query)
    scored: list[tuple[float, int, dict[str, Any], list[str]]] = []
    temporal_support = False
    rejection_reasons: dict[str, int] = {}

    for idx, raw in enumerate(source_docs):
        if not isinstance(raw, dict):
            continue
        chunk_id = str(raw.get("chunk_id") or raw.get("id") or "").strip()
        if not chunk_id:
            continue
        text = _text(raw.get("text") or raw.get("chunk_text") or "", _PACKET_EVIDENCE_TEXT_LIMIT)
        if not text:
            continue
        has_temporal = _has_temporal_source_support(raw)
        temporal_support = bool(temporal_support or has_temporal)
        score, reasons = _evidence_quality(raw, query_terms)
        scored.append((score, idx, {**raw, "text": text, "has_temporal": has_temporal}, reasons))

    if not scored:
        return [], 0, temporal_support, rejection_reasons

    scored.sort(key=lambda row: (row[0], -row[1]), reverse=True)
    accepted = [
        (score, idx, row, reasons)
        for score, idx, row, reasons in scored
        if score >= 1.0 and not (_LOW_VALUE_EVIDENCE_FLAGS & set(reasons))
    ][: _PACKET_MAX_EVIDENCE]

    def _term_hit_count(reasons: list[str]) -> int:
        for reason in reasons:
            text = str(reason)
            if text.startswith("query_terms:"):
                try:
                    return int(text.split(":", 1)[1])
                except Exception:
                    return 0
        return 0

    def _fallback_safe(score: float, reasons: list[str]) -> bool:
        if _LOW_VALUE_EVIDENCE_FLAGS & set(reasons):
            return False
        if score >= 0.0:
            return True
        required_hits = min(2, max(1, len(query_terms)))
        return _term_hit_count(reasons) >= required_hits

    # If every topical row is merely thin, keep one so the synthesis can explain
    # the limits. Explicit bibliography/front-matter/index-like rows stay out,
    # and rows with no query-term overlap plus a poor rank score stay out too.
    if not accepted:
        accepted = [
            (score, idx, row, reasons)
            for score, idx, row, reasons in scored
            if _fallback_safe(score, reasons)
        ][:1]

    accepted_keys = {(idx, str(row.get("chunk_id") or row.get("id") or "")) for _score, idx, row, _reasons in accepted}
    for _score, idx, row, reasons in scored:
        key = (idx, str(row.get("chunk_id") or row.get("id") or ""))
        if key in accepted_keys:
            continue
        reason_list = reasons or ["below_quality_floor"]
        for reason in reason_list:
            reason_key = str(reason).split(":", 1)[0] or "below_quality_floor"
            rejection_reasons[reason_key] = rejection_reasons.get(reason_key, 0) + 1

    evidence: list[dict[str, Any]] = []
    for evidence_idx, (score, _idx, row, reasons) in enumerate(accepted, start=1):
        evidence.append(
            {
                "evidence_id": f"e{evidence_idx}",
                "chunk_id": str(row.get("chunk_id") or row.get("id") or ""),
                "doc_id": str(row.get("doc_id") or ""),
                "text": _text(row.get("text") or "", _PACKET_EVIDENCE_TEXT_LIMIT),
                "source_label": _source_label_from_row(
                    row,
                    doc_id=str(row.get("doc_id") or ""),
                    chunk_id=str(row.get("chunk_id") or row.get("id") or ""),
                ),
                "source": row.get("source") or row.get("source_meta") or {},
                "heading_path": row.get("heading_path") or [],
                "score": round(score, 3),
                "quality_flags": reasons,
                "has_temporal": bool(row.get("has_temporal")),
            }
        )

    return evidence, max(0, len(scored) - len(evidence)), temporal_support, rejection_reasons


def _source_docs_from_retrieval_chunks(
    chunks: list[Any],
    *,
    max_chunks: int = _PACKET_MAX_EVIDENCE,
) -> list[dict[str, Any]]:
    """Convert shared chat retriever chunks into graph packet source rows.

    Chat retrieval already handles embedding search, optional lexical recall,
    summary chunks, graph expansion, reranking, and hydration. Graph synthesis
    should consume that final evidence pool instead of inventing a separate
    evidence universe from concept scope alone.
    """

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rank, chunk in enumerate(chunks or [], start=1):
        chunk_id = str(getattr(chunk, "chunk_id", "") or "").strip()
        doc_id = str(getattr(chunk, "doc_id", "") or "").strip()
        if not chunk_id:
            continue
        key = chunk_id or f"{doc_id}:{rank}"
        if key in seen:
            continue
        seen.add(key)
        text = str(getattr(chunk, "text", "") or getattr(chunk, "summary", "") or "")
        rows.append(
            {
                "chunk_id": chunk_id,
                "doc_id": doc_id,
                "parent_id": str(getattr(chunk, "parent_id", "") or ""),
                "corpus_id": str(getattr(chunk, "corpus_id", "") or ""),
                "text": text,
                "summary": getattr(chunk, "summary", None),
                "source_label": str(getattr(chunk, "doc_name", "") or doc_id or chunk_id),
                "source_tier": str(getattr(chunk, "source_tier", "") or "retriever"),
                "heading_path": getattr(chunk, "heading_path", None) or [],
                "score": float(getattr(chunk, "score", 0.0) or 0.0),
                "retrieval_rank": rank,
                "retriever": "shared_chat_retriever",
                "provenance": getattr(chunk, "provenance", None) or [],
            }
        )
        if len(rows) >= max_chunks:
            break
    return rows


async def _retrieve_packet_source_docs(
    db: Any,
    *,
    corpus_id: str,
    query: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Retrieve the evidence packet through the same retriever used by chat."""

    meta: dict[str, Any] = {
        "source": "shared_chat_retriever",
        "requested_tier": "qdrant_mongo_graph",
        "final_top_k": _PACKET_MAX_EVIDENCE,
        "pre_rerank_k": _PACKET_RETRIEVER_PRE_RERANK_K,
        "rerank_pool": _PACKET_RETRIEVER_RERANK_POOL,
        "neo4j_expansion_cap": _PACKET_RETRIEVER_GRAPH_EXPANSION,
        "chunks": 0,
    }
    try:
        from models.schemas import RetrievalTier
        from services.retriever import retriever_orchestrator

        retrieval = await retriever_orchestrator.retrieve(
            query=query,
            corpus_ids=[corpus_id],
            retrieval_tier=RetrievalTier.qdrant_mongo_graph,
            collections=None,
            retrieval_k=_PACKET_RETRIEVER_PRE_RERANK_K,
            rerank_enabled=True,
            ranking_query=query,
            top_k_summary=20,
            rerank_top_n=_PACKET_RETRIEVER_RERANK_POOL,
            similarity_threshold=None,
            neo4j_expansion_cap=_PACKET_RETRIEVER_GRAPH_EXPANSION,
            max_corpora_per_query=1,
            final_top_k=_PACKET_MAX_EVIDENCE,
        )
    except Exception as exc:
        logger.warning("graph packet shared retriever failed: %s", exc)
        meta.update({"status": "error", "error": str(exc)})
        return [], meta

    requested = getattr(retrieval, "requested_tier", None)
    effective = getattr(retrieval, "effective_tier", None)
    meta.update(
        {
            "status": "ok",
            "requested_tier": getattr(requested, "value", requested) or "qdrant_mongo_graph",
            "effective_tier": getattr(effective, "value", effective) or "",
            "downgrade_reason": getattr(retrieval, "downgrade_reason", None) or "",
        }
    )
    rows = _source_docs_from_retrieval_chunks(
        list(getattr(retrieval, "chunks", []) or []),
        max_chunks=_PACKET_MAX_EVIDENCE,
    )
    meta["chunks"] = len(rows)
    hydrated_trace = await _hydrate_trace_source_docs(
        db,
        {"source_docs": rows},
        corpus_id=corpus_id,
    )
    hydrated_rows = [
        row for row in (hydrated_trace.get("source_docs") or rows) if isinstance(row, dict)
    ][:_PACKET_MAX_EVIDENCE]
    meta["hydrated_chunks"] = len(hydrated_rows)
    return hydrated_rows, meta


async def _hydrate_trace_source_docs(
    db: Any,
    trace: dict[str, Any],
    *,
    corpus_id: str,
) -> dict[str, Any]:
    """Attach file labels and chunk text to the bounded source-doc receipt.

    The legacy graph scorer stores only doc_id/chunk_id/score in trace.source_docs.
    That is useful for provenance but too thin for the synthesis LLM and makes
    the UI show anonymous sources. This hydration is bounded to the already
    selected source rows, so it does not run corpus-scale retrieval.
    """

    source_docs = trace.get("source_docs") or []
    if db is None or not isinstance(source_docs, list) or not source_docs:
        return trace

    rows = [dict(row) for row in source_docs if isinstance(row, dict)]
    chunk_ids = [str(row.get("chunk_id") or row.get("id") or "").strip() for row in rows]
    chunk_ids = [cid for cid in chunk_ids if cid]
    doc_ids = {
        str(row.get("doc_id") or row.get("document_id") or "").strip()
        for row in rows
        if row.get("doc_id") or row.get("document_id")
    }

    chunk_by_id: dict[str, dict[str, Any]] = {}
    if chunk_ids:
        cursor = db["chunks"].find(
            {"corpus_id": corpus_id, "chunk_id": {"$in": chunk_ids}},
            {
                "_id": 0,
                "chunk_id": 1,
                "doc_id": 1,
                "parent_id": 1,
                "text": 1,
                "summary": 1,
                "heading_path": 1,
                "source_tier": 1,
                "page_start": 1,
                "page_end": 1,
            },
        )
        chunk_by_id = {str(row.get("chunk_id")): row async for row in cursor}
        doc_ids.update(str(row.get("doc_id")) for row in chunk_by_id.values() if row.get("doc_id"))

    doc_meta: dict[str, dict[str, Any]] = {}
    parent_by_doc: dict[str, dict[str, dict[str, Any]]] = {}
    if doc_ids:
        cursor = db["documents"].find(
            {"corpus_id": corpus_id, "doc_id": {"$in": list(doc_ids)}},
            {
                "_id": 0,
                "doc_id": 1,
                "filename": 1,
                "title": 1,
                "author": 1,
                "authors": 1,
                "publisher": 1,
                "publication_date": 1,
                "published_at": 1,
                "date_published": 1,
                "genre": 1,
                "description": 1,
                "source_path": 1,
                "source_mime": 1,
                "source_tier": 1,
                "metadata": 1,
                "document_metadata": 1,
                "source_metadata": 1,
                "ebook_metadata": 1,
                "pdf_metadata": 1,
                "schema_lens": 1,
                "created_at": 1,
                "updated_at": 1,
                "parent_chunks.parent_id": 1,
                "parent_chunks.text": 1,
                "parent_chunks.summary": 1,
                "parent_chunks.heading_path": 1,
                "parent_chunks.source_tier": 1,
                "parent_chunks.page_start": 1,
                "parent_chunks.page_end": 1,
            },
        )
        async for doc in cursor:
            did = str(doc.get("doc_id") or "")
            doc_meta[did] = doc
            parent_by_doc[did] = {
                str(parent.get("parent_id") or ""): parent
                for parent in (doc.get("parent_chunks") or [])
                if isinstance(parent, dict) and parent.get("parent_id")
            }

    hydrated: list[dict[str, Any]] = []
    for row in rows:
        chunk_id = str(row.get("chunk_id") or row.get("id") or "").strip()
        chunk = chunk_by_id.get(chunk_id, {})
        doc_id = str(row.get("doc_id") or chunk.get("doc_id") or row.get("document_id") or "").strip()
        doc = doc_meta.get(doc_id, {})
        parent_id = str(chunk.get("parent_id") or _parent_id_from_summary_chunk(chunk_id))
        parent = parent_by_doc.get(doc_id, {}).get(parent_id, {})
        source_label = _source_label_from_row(
            row,
            doc=doc,
            doc_id=doc_id,
            chunk_id=chunk_id,
        )
        text = (
            chunk.get("text")
            or chunk.get("summary")
            or parent.get("summary")
            or parent.get("text")
            or row.get("text")
            or row.get("chunk_text")
            or ""
        )
        heading_path = (
            row.get("heading_path")
            or chunk.get("heading_path")
            or parent.get("heading_path")
            or []
        )
        source_meta = _compact_source_meta(
            doc=doc,
            parent=parent,
            chunk=chunk,
            row=row,
            source_label=source_label,
            heading_path=heading_path,
        )
        hydrated.append(
            {
                **row,
                "doc_id": doc_id,
                "chunk_id": chunk_id,
                "parent_id": parent_id if parent_id else row.get("parent_id"),
                "source_label": source_label,
                "source": source_meta,
                "text": _text(text, _PACKET_EVIDENCE_TEXT_LIMIT),
                "heading_path": heading_path,
                "source_tier": (
                    chunk.get("source_tier")
                    or parent.get("source_tier")
                    or row.get("source_tier")
                    or doc.get("source_tier")
                    or ""
                ),
                "ingested_at": row.get("ingested_at") or doc.get("created_at"),
                "updated_at": row.get("updated_at") or doc.get("updated_at"),
            }
        )

    return {**trace, "source_docs": hydrated}


def _graph_shape_hint(packet: dict[str, Any]) -> dict[str, str]:
    groups = [g for g in (packet.get("communities") or []) if isinstance(g, dict)]
    edges = [e for e in (packet.get("edges") or []) if isinstance(e, dict)]
    gaps = [g for g in (packet.get("gaps") or []) if isinstance(g, dict)]
    evidence_filter = packet.get("evidence_filter") if isinstance(packet.get("evidence_filter"), dict) else {}
    if evidence_filter.get("all_rejected"):
        return {
            "label": "evidence withheld",
            "description": "Candidate chunks were rejected before synthesis, so graph structure is held back for this turn.",
            "rationale": "all_candidate_chunks_failed_quality_filter",
        }
    if not groups and not edges:
        return {
            "label": "evidence-first",
            "description": "The packet is grounded mainly in returned chunks rather than a visible graph neighborhood.",
            "rationale": "no_scoped_graph_groups",
        }

    group_sizes = [max(int(g.get("scope_count") or 0), int(g.get("size") or 0), 1) for g in groups]
    total = sum(group_sizes) or 1
    top_share = max(group_sizes) / total if group_sizes else 0.0
    bridge_count = sum(int(g.get("bridge_count") or 0) for g in groups)
    if len(groups) <= 1 or top_share >= 0.68:
        return {
            "label": "focused neighborhood",
            "description": "Most query concepts sit in one visible neighborhood.",
            "rationale": "one_query_neighborhood_dominates",
        }
    if gaps and len(edges) < max(1, len(groups) - 1):
        return {
            "label": "open bridge question",
            "description": "The packet sees multiple neighborhoods and asks where a relation still needs evidence.",
            "rationale": "gap_candidates_between_scoped_groups",
        }
    if bridge_count or len(edges) >= max(1, len(groups) - 1):
        return {
            "label": "bridged neighborhoods",
            "description": "Several query neighborhoods are connected by named concepts or bounded edges.",
            "rationale": "scoped_groups_have_gateway_edges",
        }
    return {
        "label": "dispersed neighborhoods",
        "description": "The query touches several neighborhoods without a single visible connector.",
        "rationale": "multiple_scoped_groups_few_edges",
    }


def _graph_gateway_hints(packet: dict[str, Any]) -> list[dict[str, Any]]:
    entities = [e for e in (packet.get("entities") or []) if isinstance(e, dict)]
    edges = [e for e in (packet.get("edges") or []) if isinstance(e, dict)]
    if not entities:
        return []

    names_by_id = {
        str(e.get("entity_id") or ""): _text(e.get("canonical_name") or e.get("entity_id") or "", 80)
        for e in entities
        if e.get("entity_id")
    }
    incident: dict[str, list[str]] = {}
    for edge in edges:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if not source or not target:
            continue
        incident.setdefault(source, []).append(_text(edge.get("target_name") or names_by_id.get(target) or target, 60))
        incident.setdefault(target, []).append(_text(edge.get("source_name") or names_by_id.get(source) or source, 60))

    def score(entity: dict[str, Any]) -> tuple[float, str]:
        eid = str(entity.get("entity_id") or "")
        role = str(entity.get("role") or "")
        role_score = 0.0
        if "bridge" in role or "transfer" in role:
            role_score += 8.0
        if "frontier" in role:
            role_score += 4.0
        return (
            role_score + (len(incident.get(eid, [])) * 3.0) + float(entity.get("degree") or 0),
            _text(entity.get("canonical_name") or eid, 80),
        )

    gateways: list[dict[str, Any]] = []
    for entity in sorted(entities, key=score, reverse=True):
        eid = str(entity.get("entity_id") or "")
        name = _text(entity.get("canonical_name") or eid, 80)
        if not eid or not name:
            continue
        connects = []
        for other in incident.get(eid, []):
            if other and other not in connects:
                connects.append(other)
        gateways.append(
            {
                "id": eid,
                "name": name,
                "connects": connects[:4],
                "reason": "connects packet edges" if connects else "anchors the query working set",
            }
        )
        if len(gateways) >= _PACKET_MAX_GATEWAYS:
            break
    return gateways


def _graph_gap_depths(packet: dict[str, Any]) -> list[dict[str, Any]]:
    depths = ("near", "deeper", "lateral")
    out: list[dict[str, Any]] = []
    for idx, gap in enumerate([g for g in (packet.get("gaps") or []) if isinstance(g, dict)]):
        between = [
            _text(gap.get("cluster_a_label") or gap.get("cluster_a") or "", 70),
            _text(gap.get("cluster_b_label") or gap.get("cluster_b") or "", 70),
        ]
        out.append(
            {
                "id": _text(gap.get("gap_id") or f"gap:{idx + 1}", 40),
                "depth": depths[min(idx, len(depths) - 1)],
                "between": [value for value in between if value],
                "question": _text(gap.get("question") or "", 180),
            }
        )
    return out


def _supporting_statement_hints(packet: dict[str, Any]) -> list[dict[str, str]]:
    statements: list[dict[str, str]] = []
    for idx, item in enumerate([e for e in (packet.get("evidence") or []) if isinstance(e, dict)], start=1):
        text = _text(item.get("text") or "", 260)
        if not text:
            continue
        sentence = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0]
        statements.append(
            {
                "evidence_id": str(item.get("evidence_id") or f"e{idx}"),
                "source_label": _text(
                    _source_label_from_row(
                        item,
                        doc_id=str(item.get("doc_id") or ""),
                        chunk_id=str(item.get("chunk_id") or item.get("id") or ""),
                    ),
                    80,
                ),
                "statement": _text(sentence or text, 180),
            }
        )
        if len(statements) >= _PACKET_MAX_SUPPORTING_STATEMENTS:
            break
    return statements


def _graph_hint_from_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """InfraNodus-style read of the bounded packet without new graph work."""

    shape = _graph_shape_hint(packet)
    evidence_filter = packet.get("evidence_filter") if isinstance(packet.get("evidence_filter"), dict) else {}
    if evidence_filter.get("all_rejected"):
        return {
            "shape": shape,
            "gateways": [],
            "gap_depths": [],
            "supporting_statements": [],
            "context_hint": shape.get("description") or "",
        }
    gateways = _graph_gateway_hints(packet)
    gap_depths = _graph_gap_depths(packet)
    supporting = _supporting_statement_hints(packet)
    hint_parts = [shape.get("description") or ""]
    if gateways:
        names = ", ".join(gateway["name"] for gateway in gateways[:3] if gateway.get("name"))
        if names:
            hint_parts.append(f"Follow gateway concepts: {names}.")
    if gap_depths:
        hint_parts.append("Treat gap questions as routes to inspect, not as known missing facts.")
    if supporting:
        ids = ", ".join(item["evidence_id"] for item in supporting[:3] if item.get("evidence_id"))
        if ids:
            hint_parts.append(f"Ground claims in {ids}.")
    return {
        "shape": shape,
        "gateways": gateways,
        "gap_depths": gap_depths,
        "supporting_statements": supporting,
        "context_hint": " ".join(part for part in hint_parts if part).strip(),
    }


def _build_insight_packet(result: Any, query: str, corpus_id: str) -> dict[str, Any]:
    """Assemble the bounded LLM input packet from cached legacy outputs.

    Inputs are the legacy DiscoverResult plus the original query/corpus_id.
    No graph algorithms run here — every field is a slice of data the legacy
    orchestrator already cached. Hard caps keep the prompt budget small.
    """

    trace = _coerce_dict(getattr(result, "trace", {}) or {})
    headline_payload = getattr(result, "headline", {}) or {}
    headline_text = (
        headline_payload.get("headline") if isinstance(headline_payload, dict) else ""
    ) or ""

    # ── anchors ────────────────────────────────────────────────────────────
    anchors_raw = getattr(result, "anchors", []) or []
    anchors: list[str] = []
    for anchor in anchors_raw[:8]:
        if isinstance(anchor, dict):
            label = str(anchor.get("label") or anchor.get("anchor_id") or "").strip()
        else:
            label = str(getattr(anchor, "label", "") or "").strip()
        if label and label not in anchors:
            anchors.append(label)

    # ── entities (working set + facets) ────────────────────────────────────
    graph_payload = _coerce_dict(getattr(result, "graph", {}) or {})
    graph_nodes_index: dict[str, dict[str, Any]] = {}
    for raw in graph_payload.get("nodes") or []:
        if not isinstance(raw, dict):
            continue
        nid = str(raw.get("id") or "").strip()
        if nid:
            graph_nodes_index[nid] = raw
    selected_edges_raw = trace.get("selected_edges") or []
    if not isinstance(selected_edges_raw, list):
        selected_edges_raw = []
    selected_edges_raw = [edge for edge in selected_edges_raw if isinstance(edge, dict)]
    entity_concept_map = getattr(result, "entity_concept_map", {}) or {}
    relevance_entity_ids = _query_relevant_entity_ids(
        list(graph_nodes_index.values()),
        selected_edges_raw,
        query=query,
        entity_concept_map=entity_concept_map,
    )

    working_entities = trace.get("working_entities") or []
    if not isinstance(working_entities, list):
        working_entities = []

    entities: list[dict[str, Any]] = []
    seen_entity_ids: set[str] = set()

    for raw in working_entities:
        if not isinstance(raw, dict):
            continue
        eid = str(raw.get("entity_id") or raw.get("id") or "").strip()
        if not eid or eid in seen_entity_ids:
            continue
        if relevance_entity_ids and eid not in relevance_entity_ids:
            continue
        node = graph_nodes_index.get(eid, {})
        entities.append(
            {
                "entity_id": eid,
                "canonical_name": str(raw.get("name") or node.get("label") or eid),
                "domain": str(node.get("domain") or raw.get("domain") or ""),
                "domain_type": str(node.get("domain_type") or raw.get("domain_type") or ""),
                "object_kind": str(node.get("object_kind") or raw.get("object_kind") or ""),
                "canonical_family": str(
                    node.get("canonical_family") or raw.get("canonical_family") or ""
                ),
                "degree": int(raw.get("degree") or node.get("degree") or 0),
                "role": str(raw.get("role") or node.get("emphasis") or "working"),
            }
        )
        seen_entity_ids.add(eid)
        if len(entities) >= _PACKET_MAX_ENTITIES:
            break

    # If working_entities was sparse, top up from graph nodes by degree so the
    # LLM still has names to anchor to.
    if len(entities) < _PACKET_MAX_ENTITIES:
        leftover = [
            (nid, raw)
            for nid, raw in graph_nodes_index.items()
            if nid not in seen_entity_ids
            and (not relevance_entity_ids or nid in relevance_entity_ids)
        ]
        leftover.sort(key=lambda kv: float(kv[1].get("degree") or 0), reverse=True)
        for nid, raw in leftover[: _PACKET_MAX_ENTITIES - len(entities)]:
            entities.append(
                {
                    "entity_id": nid,
                    "canonical_name": str(raw.get("label") or nid),
                    "domain": str(raw.get("domain") or ""),
                    "domain_type": str(raw.get("domain_type") or ""),
                    "object_kind": str(raw.get("object_kind") or ""),
                    "canonical_family": str(raw.get("canonical_family") or ""),
                    "degree": int(raw.get("degree") or 0),
                    "role": str(raw.get("emphasis") or "context"),
                }
            )

    # ── query-scoped concept neighborhoods ─────────────────────────────────
    # Do not hand the LLM whole-corpus buckets. Communities here are derived
    # only from entities in the bounded query working graph, using cached
    # concept/facet labels as grouping hints.
    themes_by_id = {
        str(t.get("theme_id") or ""): t
        for t in (getattr(result, "themes", []) or [])
        if isinstance(t, dict)
    }
    community_id_by_label = {
        str(c.get("label") or "").strip().lower(): str(c.get("concept_id") or "").strip()
        for c in (getattr(result, "concept_communities", []) or [])
        if isinstance(c, dict) and c.get("label") and c.get("concept_id")
    }
    scoped_groups: dict[str, dict[str, Any]] = {}
    for eid, raw in graph_nodes_index.items():
        if relevance_entity_ids and eid not in relevance_entity_ids:
            continue
        concept = entity_concept_map.get(eid, {}) or {}
        cid = str(concept.get("concept_id") or "").strip()
        label = str(
            (themes_by_id.get(cid) or {}).get("name")
            or concept.get("label")
            or raw.get("concept")
            or raw.get("canonical_family")
            or raw.get("domain_type")
            or raw.get("domain")
            or raw.get("object_kind")
            or raw.get("label")
            or "Query neighborhood"
        ).strip()
        if not cid:
            cid = community_id_by_label.get(label.lower(), "")
        gid = f"concept:{cid}" if cid else f"facet:{label.lower().replace(' ', '-')[:80] or eid}"
        group = scoped_groups.setdefault(
            gid,
            {
                "concept_id": gid,
                "label": label,
                "size": 0,
                "scope_count": 0,
                "bridge_count": 0,
                "top_entities": [],
                "degree": 0.0,
            },
        )
        group["size"] += 1
        group["scope_count"] += 1
        group["degree"] += float(raw.get("degree") or 0)
        if str(raw.get("emphasis") or "") in {"bridge", "bridge_anchor", "frontier", "transfer_hub"}:
            group["bridge_count"] += 1
        entity_label = str(raw.get("label") or eid)
        if entity_label not in group["top_entities"]:
            group["top_entities"].append(entity_label)

    communities: list[dict[str, Any]] = []
    for group in sorted(
        scoped_groups.values(),
        key=lambda row: (int(row.get("scope_count") or 0), float(row.get("degree") or 0)),
        reverse=True,
    )[:_PACKET_MAX_COMMUNITIES]:
        communities.append(
            {
                "concept_id": str(group.get("concept_id") or ""),
                "label": str(group.get("label") or ""),
                "size": int(group.get("size") or 0),
                "scope_count": int(group.get("scope_count") or 0),
                "bridge_count": int(group.get("bridge_count") or 0),
                "top_entities": [str(e) for e in (group.get("top_entities") or [])[:6] if e],
            }
        )

    # ── edges (selected_edges from trace; bounded + named) ─────────────────
    selected_edges = selected_edges_raw
    edges: list[dict[str, Any]] = []
    seen_edge_keys: set[tuple[str, str, str]] = set()
    for raw in selected_edges:
        s = str(raw.get("source") or raw.get("source_entity_id") or "").strip()
        t = str(raw.get("target") or raw.get("target_entity_id") or "").strip()
        if not s or not t:
            continue
        if relevance_entity_ids and (s not in relevance_entity_ids or t not in relevance_entity_ids):
            continue
        predicate = str(raw.get("predicate") or raw.get("relation_family") or "")
        key = (s, t, predicate)
        if key in seen_edge_keys:
            continue
        seen_edge_keys.add(key)
        s_node = graph_nodes_index.get(s, {})
        t_node = graph_nodes_index.get(t, {})
        edges.append(
            {
                "source": s,
                "target": t,
                "source_name": str(s_node.get("label") or raw.get("source_name") or s),
                "target_name": str(t_node.get("label") or raw.get("target_name") or t),
                "predicate": predicate or "related_to",
                "relation_family": str(raw.get("relation_family") or ""),
                "confidence": float(raw.get("confidence") or 0.0),
                "role": str(raw.get("role") or raw.get("emphasis") or "context"),
            }
        )
        if len(edges) >= _PACKET_MAX_EDGES:
            break

    # ── evidence chunks (with temporal-support detection) ──────────────────
    # Gaps are hypotheses, so they are gated against accepted evidence below.
    source_docs = trace.get("source_docs") or []
    if not isinstance(source_docs, list):
        source_docs = []
    raw_evidence_count = len([row for row in source_docs if isinstance(row, dict)])
    evidence, evidence_rejected, temporal_support, rejection_reasons = _curated_evidence_rows(
        source_docs,
        query=query,
    )
    evidence_all_rejected = raw_evidence_count > 0 and len(evidence) == 0
    evidence_terms = _evidence_term_index(evidence)
    edge_terms = _edge_term_index(edges)
    query_terms = _graph_relevance_terms(query)

    # ── gaps (suggested, not real Neo4j edges) ─────────────────────────────
    gaps: list[dict[str, Any]] = []
    relevant_cluster_ids = {
        str(group.get("concept_id") or "").replace("concept:", "", 1)
        for group in communities
        if str(group.get("concept_id") or "").startswith("concept:")
    }
    for gap in (getattr(result, "gaps_v2", []) or []):
        if not isinstance(gap, dict):
            continue
        include_gap, support_status = _gap_supported_by_scope(
            gap,
            relevant_cluster_ids=relevant_cluster_ids,
            query_terms=query_terms,
            evidence_terms=evidence_terms,
            edge_terms=edge_terms,
        )
        if not include_gap:
            continue
        cluster_a = str(gap.get("cluster_a") or "")
        cluster_b = str(gap.get("cluster_b") or "")
        gaps.append(
            {
                "gap_id": str(gap.get("gap_id") or ""),
                "cluster_a": cluster_a,
                "cluster_b": cluster_b,
                "cluster_a_label": str(gap.get("cluster_a_label") or ""),
                "cluster_b_label": str(gap.get("cluster_b_label") or ""),
                "question": str(gap.get("question") or ""),
                "coherence": gap.get("coherence") if isinstance(gap.get("coherence"), dict) else {},
                "anchor_concepts": [str(v) for v in (gap.get("anchor_concepts") or [])[:6] if v],
                "support_status": support_status,
            }
        )
        if len(gaps) >= _PACKET_MAX_GAPS:
            break

    # ── signals (latent topics — never call them "trends") ─────────────────
    signals: list[dict[str, Any]] = []
    relevance_terms = _graph_relevance_terms(query)
    for topic in (getattr(result, "latent_topics", []) or []):
        if not isinstance(topic, dict):
            continue
        topic_id = str(topic.get("entity_id") or "")
        topic_text = " ".join(
            str(v)
            for v in (
                topic.get("canonical_name"),
                topic.get("domain"),
                topic.get("rationale"),
            )
            if v
        ).lower()
        if (
            relevance_entity_ids
            and topic_id not in relevance_entity_ids
            and relevance_terms
            and not any(term in topic_text for term in relevance_terms)
        ):
            continue
        signals.append(
            {
                "entity_id": str(topic.get("entity_id") or ""),
                "canonical_name": str(topic.get("canonical_name") or ""),
                "domain": str(topic.get("domain") or ""),
                "mention_count": int(topic.get("mention_count") or 0),
                "doc_count": int(topic.get("doc_count") or 0),
                "degree": int(topic.get("degree") or 0),
                "rationale": _text(topic.get("rationale") or "", 220),
            }
        )
        if len(signals) >= _PACKET_MAX_SIGNALS:
            break
    if not signals:
        source_doc_rows = trace.get("source_docs") or []
        if not isinstance(source_doc_rows, list):
            source_doc_rows = []
        evidence_doc_count = len(
            {
                str(row.get("doc_id") or "")
                for row in source_doc_rows
                if isinstance(row, dict) and row.get("doc_id")
            }
        )
        for group in communities[:_PACKET_MAX_SIGNALS]:
            top_entities = [str(e) for e in (group.get("top_entities") or [])[:4] if e]
            mentions = int(group.get("scope_count") or len(top_entities) or 1)
            signal_name = str(group.get("label") or "Query-scoped signal")
            signals.append(
                {
                    "entity_id": str(group.get("concept_id") or f"signal:{len(signals) + 1}"),
                    "canonical_name": signal_name,
                    "domain": "query_scope",
                    "mention_count": mentions,
                    "doc_count": evidence_doc_count,
                    "degree": int(group.get("bridge_count") or group.get("size") or 0),
                    "rationale": _text(
                        (
                            f"Query-scoped signal: {signal_name} remained visible after relevance filtering "
                            f"with {mentions} scoped concept(s), {len(edges)} selected edge(s), "
                            f"and {len(source_doc_rows)} source chunk(s). Top entities: "
                            f"{', '.join(top_entities) if top_entities else 'none surfaced'}."
                        ),
                        220,
                    ),
                }
            )
            if len(signals) >= _PACKET_MAX_SIGNALS:
                break

    # ── weak links (provenance warnings, not gaps) ─────────────────────────
    weak_links: list[dict[str, Any]] = []
    for weak in (getattr(result, "weak_links", []) or []):
        if not isinstance(weak, dict):
            continue
        weak_source = str(weak.get("source") or "")
        weak_target = str(weak.get("target") or "")
        if relevance_entity_ids and weak_source not in relevance_entity_ids and weak_target not in relevance_entity_ids:
            continue
        weak_links.append(
            {
                "source": weak_source,
                "target": weak_target,
                "source_name": str(weak.get("source_name") or ""),
                "target_name": str(weak.get("target_name") or ""),
                "weakness_type": str(weak.get("weakness_type") or ""),
                "severity": str(weak.get("severity") or "medium"),
                "rationale": _text(weak.get("rationale") or weak.get("evidence") or "", 240),
            }
        )
        if len(weak_links) >= _PACKET_MAX_WEAK_LINKS:
            break

    # ── trace stages (already populated by _trace_with_stages) ─────────────
    stages_raw = trace.get("stages") or []
    trace_stages = [
        {
            "stage": str(stage.get("stage") or ""),
            "label": str(stage.get("label") or ""),
            "count": int(stage.get("count") or 0),
        }
        for stage in stages_raw
        if isinstance(stage, dict)
    ]

    # Sparse ⇔ no evidence chunks AND not enough graph signal to write a
    # meaningful narrative. Mirrors _insight_packet_summary_from_result so
    # the two stay in sync.
    sparse = (
        evidence_all_rejected
        or (
            len(evidence) == 0
            and len(anchors) == 0
            and len(entities) < 2
            and len(communities) < 2
            and len(edges) == 0
        )
    )

    packet = {
        "query": _text(query, 320),
        "corpus_id": corpus_id,
        "collections": _qdrant_collections_for_packet(corpus_id),
        "interpretation": _text(getattr(result, "interpretation", "") or "", 480),
        "headline": _text(headline_text, 240),
        "retrieval": trace.get("retrieval_evidence") or {},
        "anchors": anchors,
        "entities": entities,
        "communities": communities,
        "edges": edges,
        "gaps": gaps,
        "signals": signals,
        "weak_links": weak_links,
        "evidence": evidence,
        "evidence_filter": {
            "raw": raw_evidence_count,
            "accepted": len(evidence),
            "rejected": evidence_rejected,
            "rejection_reasons": rejection_reasons,
            "all_rejected": evidence_all_rejected,
            "gate_reason": (
                "all_candidate_chunks_failed_quality_filter"
                if evidence_all_rejected
                else ""
            ),
            "policy": "drop bibliography/front-matter/index-like chunks before synthesis",
        },
        "trace_stages": trace_stages,
        "sparse": sparse,
        "temporal_support": temporal_support,
    }
    packet["graph_hint"] = _graph_hint_from_packet(packet)
    return packet


def _llm_context_trace_from_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Small UI-facing receipt for the exact bounded packet sent to synthesis."""

    files_by_id: dict[str, dict[str, Any]] = {}
    chunks: list[dict[str, Any]] = []
    for item in packet.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        chunk_id = str(item.get("chunk_id") or "").strip()
        doc_id = str(item.get("doc_id") or "").strip() or "unknown-doc"
        source_label = _source_label_from_row(item, doc_id=doc_id, chunk_id=chunk_id)
        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        if doc_id not in files_by_id:
            files_by_id[doc_id] = {
                "doc_id": doc_id,
                "source_label": source_label,
                "source": source,
                "chunk_count": 0,
                "chunk_ids": [],
                "has_temporal": False,
            }
        elif source and not files_by_id[doc_id].get("source"):
            files_by_id[doc_id]["source"] = source
        files_by_id[doc_id]["chunk_count"] += 1
        if chunk_id:
            files_by_id[doc_id]["chunk_ids"].append(chunk_id)
        files_by_id[doc_id]["has_temporal"] = bool(
            files_by_id[doc_id]["has_temporal"] or item.get("has_temporal")
        )
        chunks.append(
            {
                "evidence_id": str(item.get("evidence_id") or ""),
                "chunk_id": chunk_id,
                "doc_id": doc_id,
                "source_label": source_label,
                "source": source,
                "preview": _text(item.get("text") or "", 220),
                "quality_flags": item.get("quality_flags") or [],
                "score": item.get("score"),
                "has_temporal": bool(item.get("has_temporal")),
            }
        )

    files = sorted(
        files_by_id.values(),
        key=lambda row: (-int(row.get("chunk_count") or 0), str(row.get("source_label") or "")),
    )

    user_prompt = _render_packet_user_prompt(packet)
    research_contract = _research_contract_for_prompt()

    return {
        "packet_version": "graph-insight-v1",
        "query": packet.get("query") or "",
        "collections": packet.get("collections") or {},
        "retrieval": packet.get("retrieval") or {},
        "graph_hint": packet.get("graph_hint") or {},
        "research_contract": research_contract,
        "files": files,
        "chunks": chunks,
        "prompt": {
            "system_chars": len(_SYNTHESIS_SYSTEM_PROMPT),
            "user_chars": len(user_prompt),
            "estimated_tokens": (len(_SYNTHESIS_SYSTEM_PROMPT) + len(user_prompt)) // 4,
            "preview": _text(user_prompt, 1200),
        },
        "counts": {
            "files": len(files),
            "chunks": len(chunks),
            "entities": len(packet.get("entities") or []),
            "communities": len(packet.get("communities") or []),
            "edges": len(packet.get("edges") or []),
            "gaps": len(packet.get("gaps") or []),
            "signals": len(packet.get("signals") or []),
            "weak_links": len(packet.get("weak_links") or []),
            "gateways": len((packet.get("graph_hint") or {}).get("gateways") or []),
        },
        "visibility": {
            "max_entities": _PACKET_MAX_ENTITIES,
            "max_evidence_chunks": _PACKET_MAX_EVIDENCE,
            "evidence_text_limit": _PACKET_EVIDENCE_TEXT_LIMIT,
            "evidence_filter": packet.get("evidence_filter") or {},
            "graph_hint": packet.get("graph_hint") or {},
            "temporal_support": bool(packet.get("temporal_support")),
            "sparse": bool(packet.get("sparse")),
            "claim_levels": research_contract.get("claim_levels") or [],
        },
    }


_SYNTHESIS_SYSTEM_PROMPT = (
    "You are Polymath's research synthesizer: read the supplied packet like a "
    "careful interdisciplinary researcher who wants useful nuance, not a graph "
    "dashboard. Transform stored chunks, summaries, entities, relations, and "
    "source metadata into grounded information and testable knowledge. "
    "Write a concise corpus insight synthesis from ONLY the supplied packet. "
    "Return JSON only with keys: headline, themes, bridges, gaps, "
    "emerging_signals, next_moves, evidence_notes. Each list item is "
    '{"title":str,"body":str,"evidence":[str],"related_ids":[str]}. '
    "Maximum items: themes 3, bridges 2, gaps 3, emerging_signals 3, "
    "next_moves 2, evidence_notes 2. Keep headline under 140 characters and "
    "each body under 320 characters. "
    "Prioritize bridges, gaps, and emerging_signals; themes should be short "
    "orientation frames, not the main answer. Bodies should be natural "
    "1-2 sentence research notes. For bridges, explain the mechanism, analogy, "
    "or tension being connected and why it matters for the user's query. For "
    "gaps, explain the missing relation as a testable hypothesis and name what "
    "evidence would resolve it. Ignore any gap whose support says outside or "
    "not in evidence. For emerging_signals, describe a relation pattern visible "
    "in the packet; if web_state is absent, do not make current-market or "
    "current-web claims. Distinguish direct evidence from graph structure from "
    "hypothesis. Do not frame the answer as occurrence counts, a metric report, "
    "or strong/weak scoring. Never put raw percentages, edge counts, or relation "
    "family percentages in the headline. Avoid occurrence/mention phrasing such "
    "as repeated mentions, recurring, frequent, weak, or strong unless those "
    "words are inside quoted source text. Use graph_hint as the reading lens: "
    "shape, gateway concepts, gap depth, and supporting statements. "
    "Cite supplied evidence ids like [e1] or graph structure as [graph]. "
    "Use source metadata for orientation, but only state author/date/publisher "
    "when supplied in the packet. "
    "Do not invent entities, edges, chunks, counts, files, or citations. "
    "Gaps are hypotheses, not confirmed missing edges. Weak links belong in "
    "evidence_notes only. Never say trend/trending unless temporal=true; say "
    "emerging signal or recurrence. No markdown and no slash-joined titles. "
    "If evidence is thin, write fewer items and say what is missing."
)


_SYNTHESIS_REPAIR_SYSTEM_PROMPT = (
    "Return compact valid JSON only. Use exactly these top-level keys: "
    "headline, themes, bridges, gaps, emerging_signals, next_moves, "
    "evidence_notes. headline is a string. Other fields are arrays with at "
    "most 2 items. Each item is {\"title\":str,\"body\":str,\"evidence\":[str],"
    "\"related_ids\":[str]}. Keep every body under 240 characters. No markdown."
)


def _render_packet_user_prompt(packet: dict[str, Any]) -> str:
    """Render only the curated synthesis brief, not the full trace packet."""

    compact = _compact_packet_for_prompt(packet)
    payload = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    return f"Synthesize this curated graph brief as JSON only:\n{payload}"


def _render_packet_repair_prompt(packet: dict[str, Any]) -> str:
    compact = _compact_packet_for_prompt(packet)
    payload = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    return (
        "The previous graph synthesis response was invalid or truncated. "
        "Re-synthesize the same brief as smaller valid JSON only:\n"
        f"{payload}"
    )


def _source_brief_for_prompt(item: dict[str, Any]) -> dict[str, Any]:
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    source_label = _source_label_from_row(
        item,
        doc_id=str(item.get("doc_id") or ""),
        chunk_id=str(item.get("chunk_id") or item.get("id") or ""),
    )
    brief = {
        "title": _text(source_label, 80),
        "type": _text(source.get("source_type") or "", 24),
        "section": _text(source.get("section") or "", 70),
        "page": _text(source.get("page_range") or "", 24),
        "author": _text(source.get("author") or "", 70),
        "publisher": _text(source.get("publisher") or "", 70),
        "date": _text(source.get("publication_date") or "", 40),
        "genre": _text(source.get("genre") or "", 50),
    }
    hints = source.get("hints") if isinstance(source.get("hints"), dict) else {}
    hint_brief = {
        "domains": [str(v) for v in (hints.get("domains") or [])[:2] if v],
        "relations": [str(v) for v in (hints.get("relations") or [])[:2] if v],
    }
    hint_brief = {key: value for key, value in hint_brief.items() if value}
    if hint_brief:
        brief["hints"] = hint_brief
    return {key: value for key, value in brief.items() if value not in ("", [], {}, None)}


def _compact_packet_for_prompt(packet: dict[str, Any]) -> dict[str, Any]:
    evidence_filter = packet.get("evidence_filter") if isinstance(packet.get("evidence_filter"), dict) else {}
    graph_context_allowed = not bool(evidence_filter.get("all_rejected"))
    evidence = [
        {
            "id": str(item.get("evidence_id") or f"e{idx}"),
            "source": _source_brief_for_prompt(item),
            "text": _text(item.get("text") or "", 220),
        }
        for idx, item in enumerate(packet.get("evidence") or [], start=1)
        if isinstance(item, dict)
    ]
    groups = [
        {
            "label": _text(group.get("label") or "", 70),
            "entities": [str(e) for e in (group.get("top_entities") or [])[:4] if e],
        }
        for group in (packet.get("communities") or [])[:_PACKET_MAX_COMMUNITIES]
        if graph_context_allowed and isinstance(group, dict)
    ]
    edges = [
        {
            "s": _text(edge.get("source_name") or edge.get("source") or "", 45),
            "p": _text(edge.get("predicate") or "", 35),
            "t": _text(edge.get("target_name") or edge.get("target") or "", 45),
            "role": _text(edge.get("role") or edge.get("relation_family") or "", 35),
            "conf": round(float(edge.get("confidence") or 0.0), 2),
        }
        for edge in (packet.get("edges") or [])[:_PACKET_MAX_EDGES]
        if graph_context_allowed and isinstance(edge, dict)
    ]
    gaps = [
        {
            "id": _text(gap.get("gap_id") or f"g{idx}", 24),
            "between": [
                _text(gap.get("cluster_a_label") or gap.get("cluster_a") or "", 50),
                _text(gap.get("cluster_b_label") or gap.get("cluster_b") or "", 50),
            ],
            "q": _text(gap.get("question") or "", 150),
            "support": _text(gap.get("support_status") or "", 50),
            "basis": {
                "shared_terms": [
                    str(v)
                    for v in ((gap.get("coherence") or {}).get("shared_terms") or [])[:4]
                    if v
                ],
                "shared_neighbors": [
                    str(v)
                    for v in ((gap.get("coherence") or {}).get("shared_neighbors") or [])[:4]
                    if v
                ],
                "anchors": [str(v) for v in (gap.get("anchor_concepts") or [])[:4] if v],
            },
        }
        for idx, gap in enumerate((packet.get("gaps") or [])[:_PACKET_MAX_GAPS], start=1)
        if graph_context_allowed and isinstance(gap, dict) and gap.get("question")
    ]
    signals = [
        {
            "id": _text(sig.get("entity_id") or f"s{idx}", 32),
            "name": _text(sig.get("canonical_name") or "", 60),
            "why": _text(sig.get("rationale") or "", 120),
        }
        for idx, sig in enumerate((packet.get("signals") or [])[:_PACKET_MAX_SIGNALS], start=1)
        if graph_context_allowed and isinstance(sig, dict)
    ]
    gateway_focus = [
        {
            "edge": f"{edge['s']} -> {edge['p']} -> {edge['t']}",
            "read_as": "connector to explain",
        }
        for edge in edges[:5]
        if edge.get("s") and edge.get("t")
    ]
    graph_hint = packet.get("graph_hint") if isinstance(packet.get("graph_hint"), dict) else {}
    compact_graph_hint = {
        "shape": graph_hint.get("shape") or {},
        "gateways": (graph_hint.get("gateways") or [])[:3],
        "gap_depths": (graph_hint.get("gap_depths") or [])[:3],
        "context_hint": _text(graph_hint.get("context_hint") or "", 360),
    }
    warnings = [
        _text(w.get("rationale") or w.get("weakness_type") or "", 120)
        for w in (packet.get("weak_links") or [])[:_PACKET_MAX_WEAK_LINKS]
        if graph_context_allowed and isinstance(w, dict)
    ]
    return {
        "q": packet.get("query") or "",
        "retrieval": packet.get("retrieval") or {},
        "temporal": bool(packet.get("temporal_support")),
        "sparse": bool(packet.get("sparse")),
        "research_contract": _research_contract_for_prompt(),
        "synthesis_priority": {
            "primary": ["bridges", "gaps", "emerging_signals"],
            "themes": "brief framing only",
            "web_state": "absent; corpus-only current-state claims are not allowed",
        },
        "anchors": [str(a) for a in (packet.get("anchors") or [])[:5] if a],
        "groups": groups,
        "evidence": evidence,
        "edges": edges,
        "gateway_focus": gateway_focus,
        "graph_hint": compact_graph_hint,
        "gaps": gaps,
        "signals": signals,
        "warnings": warnings,
        "filter": evidence_filter,
        "quality_gate": (
            "graph context withheld because all candidate chunks failed the evidence-quality filter"
            if evidence_filter.get("all_rejected")
            else ""
        ),
    }


def _research_contract_for_prompt() -> dict[str, Any]:
    return {
        "job": "turn stored chunks, summaries, entities, relations, and source metadata into grounded research insight",
        "claim_levels": ["observed evidence", "graph structure", "testable hypothesis"],
        "avoid": "metric narration, whole-corpus trivia, or unrelated cross-domain jumps",
    }


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        # Drop opening fence + optional language hint.
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text, count=1)
        if text.endswith("```"):
            text = text[: -3]
    return text.strip()


async def _resolve_graph_model(
    user_id: Optional[str],
    model_override: Optional[str],
) -> dict[str, Any]:
    """Resolve the graph synthesis model the same way chat resolves model picks.

    The frontend stores selected pool/profile entries as `pool:<id>` or
    `profile:<id>`. LiteLLM cannot use those opaque ids directly, so graph
    synthesis must translate them to concrete model credentials first.
    """

    model = (model_override or "").strip()
    if user_id and (model.startswith("pool:") or model.startswith("profile:")):
        _prefix, _, entry_id = model.partition(":")
        try:
            from services.query_model_resolver import resolve_by_entry_id

            resolved = await resolve_by_entry_id(user_id, entry_id)
        except Exception as exc:
            logger.warning("Graph synthesis model resolution failed for %s: %s", model, exc)
            resolved = None
        if resolved:
            return {
                "model": resolved.get("model") or None,
                "api_base": resolved.get("api_base"),
                "api_key": resolved.get("api_key"),
                "extra_params": resolved.get("extra_params") or {},
                "source": model,
            }
        logger.warning(
            "Graph synthesis model reference %s was not found for user=%s; falling back to default.",
            model,
            user_id,
        )
        model = ""

    if not model and user_id:
        try:
            from services.query_model_resolver import resolve as resolve_query_model

            resolved = await resolve_query_model(user_id, "query")
        except Exception as exc:
            logger.debug("Graph synthesis query model preference lookup failed: %s", exc)
            resolved = None
        if resolved:
            return {
                "model": resolved.get("model") or None,
                "api_base": resolved.get("api_base"),
                "api_key": resolved.get("api_key"),
                "extra_params": resolved.get("extra_params") or {},
                "source": "query_pref",
            }

    if model.startswith("pool:") or model.startswith("profile:"):
        logger.warning(
            "Graph synthesis received unresolved model reference %s without a user id; falling back to default.",
            model,
        )
        model = ""

    return {
        "model": model or None,
        "api_base": None,
        "api_key": None,
        "extra_params": {},
        "source": "override" if model else "default",
    }


async def _call_llm_synthesis(
    packet: dict[str, Any],
    *,
    model_override: Optional[str],
    user_id: Optional[str] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Call the synthesis LLM. Returns (payload_dict, fallback_reason).

    Every graph query send/Enter path attempts this call, even when the packet
    is sparse. Sparse evidence is content the model must honestly discuss, not
    a reason to let deterministic templates take over. Deterministic fallback
    is reserved for request, parse, or schema failure.
    """

    try:
        from services.llm import llm_service
    except Exception as exc:  # pragma: no cover — only fires in broken envs
        logger.warning("synthesis llm_service import failed: %s", exc)
        return None, "llm_import_failure"

    messages = [
        {"role": "system", "content": _SYNTHESIS_SYSTEM_PROMPT},
        {"role": "user", "content": _render_packet_user_prompt(packet)},
    ]
    creds = await _resolve_graph_model(user_id, model_override)
    extra: dict[str, Any] = {
        **(creds.get("extra_params") or {}),
        "response_format": {"type": "json_object"},
    }
    evidence_items = [
        item for item in (packet.get("evidence") or []) if isinstance(item, dict)
    ]
    logger.info(
        "Graph synthesis LLM call: model=%s source=%s sys=%dchars usr=%dchars total≈%dtok "
        "files=%d chunks=%d collections=%s",
        creds["model"] or "(selected/default)",
        creds.get("source") or "",
        len(messages[0]["content"]),
        len(messages[1]["content"]),
        (len(messages[0]["content"]) + len(messages[1]["content"])) // 4,
        len({str(item.get("doc_id") or "") for item in evidence_items if item.get("doc_id")}),
        len(evidence_items),
        ",".join((packet.get("collections") or {}).values()),
    )

    try:
        raw = await llm_service.complete_sync(
            messages=messages,
            model=creds["model"],
            temperature=_SYNTHESIS_TEMPERATURE,
            max_tokens=_SYNTHESIS_MAX_TOKENS,
            api_base=creds.get("api_base"),
            api_key=creds.get("api_key"),
            timeout=_SYNTHESIS_TIMEOUT_SECONDS,
            extra_params=extra,
        )
    except Exception as exc:
        logger.warning("synthesis LLM call failed: %s", exc)
        return None, "llm_request_failure"

    if not raw or not raw.strip():
        logger.warning("synthesis LLM returned empty response")
        return None, "llm_empty_response"

    try:
        decoded = json.loads(_strip_code_fences(raw))
    except Exception as exc:
        logger.warning(
            "synthesis JSON decode failed (%s); attempting compact retry; raw_head=%r",
            exc,
            raw[:160],
        )
        try:
            raw = await llm_service.complete_sync(
                messages=[
                    {"role": "system", "content": _SYNTHESIS_REPAIR_SYSTEM_PROMPT},
                    {"role": "user", "content": _render_packet_repair_prompt(packet)},
                ],
                model=creds["model"],
                temperature=0.1,
                max_tokens=min(1200, _SYNTHESIS_MAX_TOKENS),
                api_base=creds.get("api_base"),
                api_key=creds.get("api_key"),
                timeout=_SYNTHESIS_TIMEOUT_SECONDS,
                extra_params=extra,
            )
        except Exception as retry_exc:
            logger.warning("synthesis JSON compact retry failed: %s", retry_exc)
            return None, "json_parse_failure"
        try:
            decoded = json.loads(_strip_code_fences(raw or ""))
        except Exception as retry_exc:
            logger.warning(
                "synthesis JSON compact retry decode failed (%s); raw_head=%r",
                retry_exc,
                (raw or "")[:160],
            )
            return None, "json_parse_failure"

    if not isinstance(decoded, dict):
        logger.warning("synthesis JSON was not an object: %r", type(decoded).__name__)
        return None, "json_shape_failure"

    decoded = _coerce_synthesis_shape(decoded)

    try:
        from models.schemas import AutoSynthesisPayload  # local import to avoid cycle
    except Exception as exc:  # pragma: no cover
        logger.warning("AutoSynthesisPayload import failed: %s", exc)
        return None, "schema_import_failure"

    try:
        validated = AutoSynthesisPayload.model_validate(decoded)
    except Exception as exc:
        logger.warning("synthesis payload failed validation: %s", exc)
        return None, "validation_failure"

    payload_dict = validated.model_dump()
    payload_dict = _scrub_synthesis_payload(payload_dict, packet)
    return payload_dict, None


_SYNTHESIS_LIST_FIELDS = (
    "themes",
    "bridges",
    "gaps",
    "emerging_signals",
    "next_moves",
    "evidence_notes",
)


def _coerce_synthesis_shape(decoded: dict[str, Any]) -> dict[str, Any]:
    """Normalize useful-but-imperfect JSON before strict Pydantic validation."""

    payload = dict(decoded)
    payload["headline"] = _text(
        payload.get("headline") or payload.get("title") or payload.get("summary") or "",
        280,
    )

    for field in _SYNTHESIS_LIST_FIELDS:
        value = payload.get(field, [])
        if value is None:
            payload[field] = []
        elif isinstance(value, list):
            payload[field] = [
                item for item in (_coerce_synthesis_item(item, field) for item in value) if item
            ]
        else:
            item = _coerce_synthesis_item(value, field)
            payload[field] = [item] if item else []

    return payload


def _coerce_synthesis_item(value: Any, field: str) -> Optional[dict[str, Any]]:
    if isinstance(value, dict):
        title_source = value.get("title") or value.get("heading") or value.get("name")
        body_source = value.get("body") or value.get("summary") or value.get("text") or value.get("content")
        body = _text(body_source or title_source or "", 1200)
        if not body:
            return None
        title = _text(title_source or _synthesis_title_from_text(body, field), 180)
        evidence = value.get("evidence") or value.get("evidence_refs") or value.get("sources") or []
        related_ids = value.get("related_ids") or value.get("ids") or value.get("node_ids") or []
        return {
            "title": title,
            "body": body,
            "evidence": _string_list(evidence)[:6],
            "related_ids": _string_list(related_ids)[:6],
        }

    body = _text(value, 1200)
    if not body:
        return None
    return {
        "title": _synthesis_title_from_text(body, field),
        "body": body,
        "evidence": ["graph packet"],
        "related_ids": [],
    }


def _synthesis_title_from_text(text: str, field: str) -> str:
    first_clause = re.split(r"(?<=[.!?])\s+|[:;]\s+", text, maxsplit=1)[0]
    title = _text(first_clause, 90).strip(" -")
    if title:
        return title
    return field.replace("_", " ").title()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float, bool)):
        return [str(value)]
    if isinstance(value, dict):
        return [str(v) for v in value.values() if v]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value if v]
    return [str(value)]


_SLASH_TITLE_RE = re.compile(r"\s*/\s*")
_TREND_BODY_RE = re.compile(r"\b[Tt]rends?\b")
_TRENDING_BODY_RE = re.compile(r"\b[Tt]rending\b")
_METRIC_HEADLINE_RE = re.compile(
    r"(\d+(?:\.\d+)?%|\b(edge|edges|density|modularity|pagerank|weakassociation|operational)\b)",
    re.IGNORECASE,
)


def _research_headline_from_packet(packet: dict[str, Any]) -> str:
    query = _text(packet.get("query") or "Graph query", 70)
    groups = [
        str(group.get("label") or "")
        for group in (packet.get("communities") or [])[:2]
        if isinstance(group, dict) and group.get("label")
    ]
    edges = [
        f"{edge.get('source_name') or edge.get('source')} -> {edge.get('target_name') or edge.get('target')}"
        for edge in (packet.get("edges") or [])[:1]
        if isinstance(edge, dict)
    ]
    if edges:
        return _text(f"{query}: evidence-backed bridge around {edges[0]}", 140)
    if groups:
        return _text(f"{query}: research read across {', '.join(groups[:2])}", 140)
    return _text(f"{query}: evidence-grounded graph read", 140)


def _scrub_synthesis_payload(
    payload: dict[str, Any], packet: dict[str, Any]
) -> dict[str, Any]:
    """Apply non-negotiable hygiene rules even if the LLM ignored them."""

    temporal = bool(packet.get("temporal_support"))

    def _clean_item(item: dict[str, Any]) -> dict[str, Any]:
        title = _SLASH_TITLE_RE.sub(" ", _text(item.get("title") or "", 160))
        body = _text(item.get("body") or "", 1200)
        if not temporal:
            body = _TREND_BODY_RE.sub("signals", body)
            body = _TRENDING_BODY_RE.sub("emerging", body)
        return {
            "title": title,
            "body": body,
            "evidence": [str(e) for e in (item.get("evidence") or []) if e][:6],
            "related_ids": [str(r) for r in (item.get("related_ids") or []) if r][:6],
        }

    headline = _text(payload.get("headline") or "", 280)
    if not headline or _METRIC_HEADLINE_RE.search(headline):
        headline = _research_headline_from_packet(packet)
    cleaned = {"headline": headline}
    for key in ("themes", "bridges", "gaps", "emerging_signals", "next_moves", "evidence_notes"):
        cleaned[key] = [
            _clean_item(item) for item in (payload.get(key) or []) if isinstance(item, dict)
        ]
    return cleaned


def _fallback_evidence_note(reason: str) -> dict[str, Any]:
    if reason == "sparse_evidence":
        body = (
            "The graph packet for this query was sparse: not enough anchored chunks, "
            "concept communities, or bounded edges to write a synthesized narrative. "
            "The structural read below was assembled deterministically from the cached "
            "graph metrics so the trail is still inspectable."
        )
        title = "Sparse evidence"
    elif reason in {"json_parse_failure", "json_shape_failure", "validation_failure"}:
        body = (
            "The synthesis model returned text that did not validate against the "
            "AutoSynthesisPayload schema, so the deterministic graph-derived view is "
            "shown instead. Re-running the query with a different model usually fixes this."
        )
        title = "Model output rejected"
    elif reason in {"llm_request_failure", "llm_empty_response", "llm_import_failure", "schema_import_failure"}:
        body = (
            "The synthesis model could not be reached for this turn. The deterministic "
            "graph-derived view below was rendered from cached metrics."
        )
        title = "Synthesis model unavailable"
    else:
        body = (
            "Falling back to the deterministic graph-derived view; see the evidence trace "
            "for what was actually loaded."
        )
        title = "Deterministic fallback"
    return {
        "title": title,
        "body": body,
        "evidence": [f"fallback:{reason}"],
        "related_ids": [],
    }


def _snapshot_from_result(result: Any) -> dict[str, Any]:
    keys = [
        "session_id", "corpus_id", "query", "mode", "interpretation",
        "frontier", "analogies", "bridges", "weak_links", "transfers",
        "questions", "strategic_read", "intent_profile", "atomic_trace",
        "socratic_prompts", "metrics", "domain_map_summary", "graph",
        "anchors", "concept_communities", "entity_concept_map", "headline",
        "themes", "bridges_v2", "gaps_v2", "latent_topics", "tensions",
        "trace", "auto_synthesis", "insight_packet_summary", "context_graph",
    ]
    return {key: getattr(result, key, None) for key in keys}


async def _persist_enriched_turn(db: Any, result: Any) -> None:
    if db is None:
        return
    try:
        await db["graph_sessions"].update_one(
            {"session_id": result.session_id, "turns.query": result.query},
            {"$set": {"turns.$.response": _snapshot_from_result(result)}},
        )
    except Exception as exc:
        logger.warning("discover enriched turn persistence skipped: %s", exc)


async def discover(
    *,
    qdrant,
    neo4j_driver,
    db,
    corpus_id: str,
    query: str,
    mode: str = "auto",
    session_id: Optional[str] = None,
    user_id: Optional[str] = None,
    model_override: Optional[str] = None,
    agentic: bool = False,
) -> Any:
    """Auto-Synthesis Mission Control wrapper.

    The legacy orchestrator still owns cache-safe graph scoping and card
    construction. This wrapper makes mode compatibility-only and adds the new
    narrative/context-map contract without touching Chat RAG.
    """
    if _legacy is None or not hasattr(_legacy, "discover"):
        raise RuntimeError(_LEGACY_MISSING_MESSAGE)
    started_at = _time.perf_counter()
    result = await _legacy.discover(
        qdrant=qdrant,
        neo4j_driver=neo4j_driver,
        db=db,
        corpus_id=corpus_id,
        query=query,
        mode="auto",
        session_id=session_id,
        user_id=user_id,
        model_override=model_override,
        agentic=False,
    )
    legacy_done_at = _time.perf_counter()
    # `mode` is compatibility-only: callers may still send it and we echo it
    # back, but discover always behaves as auto-synthesis.
    result.mode = mode or "auto"
    result.trace = _trace_with_stages(getattr(result, "trace", {}) or {})
    legacy_source_docs = list(result.trace.get("source_docs") or []) if isinstance(result.trace, dict) else []
    retrieved_source_docs, retrieval_meta = await _retrieve_packet_source_docs(
        db,
        corpus_id=corpus_id,
        query=query,
    )
    if isinstance(result.trace, dict):
        result.trace["graph_scope_source_docs"] = legacy_source_docs
        result.trace["retrieval_evidence"] = retrieval_meta
        result.trace["source_docs"] = retrieved_source_docs
    result.trace = await _hydrate_trace_source_docs(db, result.trace, corpus_id=corpus_id)

    packet = _build_insight_packet(result, query=query, corpus_id=corpus_id)
    if isinstance(result.trace, dict):
        result.trace["source_docs_raw"] = result.trace.get("source_docs") or []
        result.trace["source_docs"] = packet.get("evidence") or []
        result.trace["evidence_filter"] = packet.get("evidence_filter") or {}
        result.trace["graph_hint"] = packet.get("graph_hint") or {}
    result.trace = {
        **(result.trace or {}),
        "llm_context": _llm_context_trace_from_packet(packet),
    }
    packet_done_at = _time.perf_counter()
    llm_payload, fallback_reason = await _call_llm_synthesis(
        packet, model_override=model_override, user_id=user_id
    )
    llm_done_at = _time.perf_counter()

    if llm_payload is not None:
        result.auto_synthesis = llm_payload
        synthesis_source = "llm"
    else:
        deterministic = _auto_synthesis_from_result(result)
        # Make the fallback reason explicit at the top of the evidence notes so
        # the reader knows the LLM didn't write the prose.
        evidence_notes = list(deterministic.get("evidence_notes") or [])
        evidence_notes.insert(0, _fallback_evidence_note(fallback_reason or "unknown"))
        deterministic["evidence_notes"] = evidence_notes[:6]
        result.auto_synthesis = deterministic
        synthesis_source = f"fallback:{fallback_reason or 'unknown'}"

    result.insight_packet_summary = _insight_packet_summary_from_result(result)
    if fallback_reason:
        # Mirror the fallback reason into the summary so the UI can flag it.
        ips = dict(result.insight_packet_summary or {})
        ips["fallback_reason"] = fallback_reason
        result.insight_packet_summary = ips

    result.context_graph = _context_graph_from_result(result)
    total_done_at = _time.perf_counter()
    try:
        result.trace.setdefault("llm_context", {})["timings_ms"] = {
            "legacy_scope": round((legacy_done_at - started_at) * 1000, 1),
            "packet": round((packet_done_at - legacy_done_at) * 1000, 1),
            "synthesis": round((llm_done_at - packet_done_at) * 1000, 1),
            "total_before_persist": round((total_done_at - started_at) * 1000, 1),
        }
    except Exception:
        pass
    logger.info(
        "discover auto_packet source=%s themes=%d bridges=%d gaps=%d signals=%d "
        "context_nodes=%d context_links=%d packet_entities=%d packet_evidence=%d "
        "sparse=%s temporal=%s timings=legacy_scope:%.2fs packet:%.2fs llm:%.2fs total:%.2fs "
        "corpus=%s q=%r",
        synthesis_source,
        len(result.auto_synthesis.get("themes") or []),
        len(result.auto_synthesis.get("bridges") or []),
        len(result.auto_synthesis.get("gaps") or []),
        len(result.auto_synthesis.get("emerging_signals") or []),
        len(result.context_graph.get("nodes") or []),
        len(result.context_graph.get("links") or []),
        len(packet.get("entities") or []),
        len(packet.get("evidence") or []),
        packet.get("sparse"),
        packet.get("temporal_support"),
        legacy_done_at - started_at,
        packet_done_at - legacy_done_at,
        llm_done_at - packet_done_at,
        total_done_at - started_at,
        corpus_id[:8],
        query[:80],
    )
    await _persist_enriched_turn(db, result)
    return result

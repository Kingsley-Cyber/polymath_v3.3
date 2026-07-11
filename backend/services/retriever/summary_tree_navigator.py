"""Query-time navigation over the existing L2-L4 summary hierarchy.

Document profiles establish scope in Tier 0. Within each routed document this
module scores sections first, then only the rollups below selected sections.
This bounded adaptive descent preserves RAPTOR's multi-level retrieval benefit
without embedding every tree node on each request. Selected nodes are always
descended to L1 parent IDs; tree summaries guide discovery and are never
returned as final citation evidence.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from services.conversation import conversation_service
from services.embedder import embed_queries
from services.retriever.tier0_router import DocumentRoute

logger = logging.getLogger(__name__)

EmbedFn = Callable[[list[str], dict[str, Any] | None], Awaitable[list[list[float]]]]


@dataclass(frozen=True)
class TreeNodeCandidate:
    node_id: str
    node_type: str
    score: float
    token_estimate: int = 0


@dataclass(frozen=True)
class SummaryTreeRoute:
    lane_id: str
    corpus_id: str
    doc_id: str
    selected_node_ids: tuple[str, ...]
    section_ids: tuple[str, ...]
    rollup_ids: tuple[str, ...]
    parent_ids: tuple[str, ...]


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0.0 or right_norm <= 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def select_collapsed_tree_nodes(
    candidates: list[TreeNodeCandidate],
    *,
    min_score: float = 0.24,
    relative_margin: float = 0.18,
    cliff_min_gap: float = 0.07,
    min_keep: int = 1,
    max_keep: int = 5,
    max_tokens: int = 1200,
) -> list[TreeNodeCandidate]:
    """Select the relevant abstraction levels under a bounded token budget."""

    ordered = sorted(candidates, key=lambda item: (-item.score, item.node_id))
    if not ordered:
        return []
    floor = max(float(min_score), float(ordered[0].score) - float(relative_margin))
    eligible = [item for item in ordered if float(item.score) >= floor]
    if not eligible:
        eligible = ordered[: max(1, int(min_keep))]
    eligible = eligible[: max(1, int(max_keep))]
    minimum = min(max(1, int(min_keep)), len(eligible))
    for index in range(minimum - 1, len(eligible) - 1):
        if eligible[index].score - eligible[index + 1].score >= cliff_min_gap:
            eligible = eligible[: index + 1]
            break

    selected: list[TreeNodeCandidate] = []
    used_tokens = 0
    for item in eligible:
        estimate = max(1, int(item.token_estimate or 1))
        if selected and used_tokens + estimate > max(1, int(max_tokens)):
            continue
        selected.append(item)
        used_tokens += estimate
    return selected


class SummaryTreeNavigator:
    async def navigate(
        self,
        *,
        lane_vectors: dict[str, list[float] | None],
        document_routes: dict[str, list[DocumentRoute]],
        embedding_config: dict[str, Any] | None = None,
        db: Any | None = None,
        embed_fn: EmbedFn = embed_queries,
        max_nodes_per_document: int = 180,
        max_parent_ids_per_document: int = 48,
    ) -> tuple[dict[str, list[SummaryTreeRoute]], dict[str, Any]]:
        """Resolve routed documents to source parent neighborhoods."""

        diagnostics: dict[str, Any] = {
            "enabled": True,
            "strategy": "document_gated_adaptive_tree_descent",
            "routes": {},
            "node_count": 0,
            "failures": [],
        }
        database = db if db is not None else conversation_service._db
        usable_lanes = {
            lane_id: vector
            for lane_id, vector in lane_vectors.items()
            if vector is not None and document_routes.get(lane_id)
        }
        route_pairs = {
            (str(route.corpus_id), str(route.doc_id))
            for lane_id in usable_lanes
            for route in document_routes.get(lane_id, [])
            if route.corpus_id and route.doc_id
        }
        if database is None or not usable_lanes or not route_pairs:
            diagnostics["reason"] = "missing_database_vectors_or_document_routes"
            return {}, diagnostics

        pair_filters = [
            {"corpus_id": corpus_id, "doc_id": doc_id}
            for corpus_id, doc_id in sorted(route_pairs)
        ]
        try:
            rows = (
                await database["summary_tree"]
                .find(
                    {
                        "$and": [
                            {"$or": pair_filters},
                            {"node_type": {"$in": ["section", "rollup"]}},
                            {"summary": {"$type": "string", "$ne": ""}},
                        ]
                    },
                    {
                        "_id": 0,
                        "node_id": 1,
                        "node_type": 1,
                        "corpus_id": 1,
                        "doc_id": 1,
                        "summary": 1,
                        "parent_ids": 1,
                        "child_node_ids": 1,
                        "section_range": 1,
                    },
                )
                .to_list(length=max(500, len(route_pairs) * max_nodes_per_document * 2))
            )
        except Exception as exc:  # noqa: BLE001 - retrieval must degrade cleanly
            diagnostics["reason"] = "summary_tree_read_failed"
            diagnostics["failures"].append(f"{type(exc).__name__}: {exc}"[:240])
            return {}, diagnostics

        sections_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
        rollups_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
        rows_by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            node_id = str(row.get("node_id") or "")
            pair = (str(row.get("corpus_id") or ""), str(row.get("doc_id") or ""))
            if not node_id or pair not in route_pairs:
                continue
            buckets = (
                sections_by_pair
                if str(row.get("node_type") or "") == "section"
                else rollups_by_pair
            )
            bucket = buckets.setdefault(pair, [])
            if len(bucket) >= max(1, int(max_nodes_per_document)):
                continue
            bucket.append(row)
            rows_by_id[node_id] = row
        diagnostics["node_count"] = len(rows_by_id)
        diagnostics["section_count"] = sum(
            len(values) for values in sections_by_pair.values()
        )
        diagnostics["rollup_count"] = sum(
            len(values) for values in rollups_by_pair.values()
        )
        if not rows_by_id:
            diagnostics["reason"] = "no_summary_tree_nodes"
            return {}, diagnostics

        section_ids = sorted(
            str(row["node_id"])
            for values in sections_by_pair.values()
            for row in values
        )
        try:
            section_vectors = await embed_fn(
                [
                    str(rows_by_id[node_id].get("summary") or "")
                    for node_id in section_ids
                ],
                embedding_config,
            )
        except Exception as exc:  # noqa: BLE001 - document routing remains available
            diagnostics["reason"] = "summary_tree_embedding_failed"
            diagnostics["failures"].append(f"{type(exc).__name__}: {exc}"[:240])
            return {}, diagnostics
        vectors_by_node_id = dict(zip(section_ids, section_vectors))

        selected_sections: dict[tuple[str, str, str], list[TreeNodeCandidate]] = {}
        required_rollup_ids: set[str] = set()
        for lane_id, query_vector in usable_lanes.items():
            for document_route in document_routes.get(lane_id, []):
                pair = (str(document_route.corpus_id), str(document_route.doc_id))
                section_rows = sections_by_pair.get(pair) or []
                section_candidates = [
                    TreeNodeCandidate(
                        node_id=str(row["node_id"]),
                        node_type="section",
                        score=_cosine(
                            query_vector or [],
                            vectors_by_node_id.get(str(row["node_id"])) or [],
                        ),
                        token_estimate=max(
                            1, len(str(row.get("summary") or "").split())
                        ),
                    )
                    for row in section_rows
                ]
                selected = select_collapsed_tree_nodes(
                    section_candidates,
                    max_keep=5,
                    max_tokens=1200,
                )
                selected_sections[(lane_id, pair[0], pair[1])] = selected
                for section in selected:
                    child_ids = {
                        str(value)
                        for value in (rows_by_id.get(section.node_id) or {}).get(
                            "child_node_ids", []
                        )
                        if str(value) and str(value) in rows_by_id
                    }
                    required_rollup_ids.update(child_ids)
                if not selected or not any(
                    str(value) in rows_by_id
                    for section in selected
                    for value in (rows_by_id.get(section.node_id) or {}).get(
                        "child_node_ids", []
                    )
                ):
                    required_rollup_ids.update(
                        str(row["node_id"]) for row in (rollups_by_pair.get(pair) or [])
                    )

        rollup_ids = sorted(
            node_id for node_id in required_rollup_ids if node_id in rows_by_id
        )
        if rollup_ids:
            try:
                rollup_vectors = await embed_fn(
                    [
                        str(rows_by_id[node_id].get("summary") or "")
                        for node_id in rollup_ids
                    ],
                    embedding_config,
                )
                vectors_by_node_id.update(zip(rollup_ids, rollup_vectors))
            except (
                Exception
            ) as exc:  # noqa: BLE001 - document routing remains available
                diagnostics["reason"] = "summary_tree_rollup_embedding_failed"
                diagnostics["failures"].append(f"{type(exc).__name__}: {exc}"[:240])
                return {}, diagnostics
        diagnostics["embedded_section_count"] = len(section_ids)
        diagnostics["embedded_rollup_count"] = len(rollup_ids)

        output: dict[str, list[SummaryTreeRoute]] = {}
        for lane_id, query_vector in usable_lanes.items():
            lane_routes: list[SummaryTreeRoute] = []
            for document_route in document_routes.get(lane_id, []):
                pair = (str(document_route.corpus_id), str(document_route.doc_id))
                selected_section_nodes = selected_sections.get(
                    (lane_id, pair[0], pair[1]), []
                )
                allowed_rollup_ids = {
                    str(value)
                    for section in selected_section_nodes
                    for value in (rows_by_id.get(section.node_id) or {}).get(
                        "child_node_ids", []
                    )
                    if str(value) and str(value) in rows_by_id
                }
                if not allowed_rollup_ids:
                    allowed_rollup_ids = {
                        str(row["node_id"]) for row in (rollups_by_pair.get(pair) or [])
                    }
                rollup_candidates = [
                    TreeNodeCandidate(
                        node_id=node_id,
                        node_type="rollup",
                        score=_cosine(
                            query_vector or [],
                            vectors_by_node_id.get(node_id) or [],
                        ),
                        token_estimate=max(
                            1,
                            len(
                                str(
                                    (rows_by_id.get(node_id) or {}).get("summary") or ""
                                ).split()
                            ),
                        ),
                    )
                    for node_id in sorted(allowed_rollup_ids)
                    if node_id in vectors_by_node_id
                ]
                selected_rollups = select_collapsed_tree_nodes(
                    rollup_candidates,
                    min_keep=1,
                    max_keep=4,
                    max_tokens=900,
                )
                if not selected_rollups:
                    continue

                selected_section_ids = [item.node_id for item in selected_section_nodes]
                selected_rollup_ids = [item.node_id for item in selected_rollups]
                selected_ids = [*selected_section_ids, *selected_rollup_ids]

                parent_ids: list[str] = []
                for rollup_id in selected_rollup_ids:
                    parent_ids.extend(
                        str(value)
                        for value in (rows_by_id.get(rollup_id) or {}).get(
                            "parent_ids", []
                        )
                        if str(value)
                    )
                parent_ids = list(dict.fromkeys(parent_ids))[
                    : max(1, int(max_parent_ids_per_document))
                ]
                if not parent_ids:
                    continue
                lane_routes.append(
                    SummaryTreeRoute(
                        lane_id=lane_id,
                        corpus_id=pair[0],
                        doc_id=pair[1],
                        selected_node_ids=tuple(selected_ids),
                        section_ids=tuple(selected_section_ids),
                        rollup_ids=tuple(selected_rollup_ids),
                        parent_ids=tuple(parent_ids),
                    )
                )
            output[lane_id] = lane_routes

        diagnostics["routes"] = {
            lane_id: [
                {
                    "corpus_id": route.corpus_id,
                    "doc_id": route.doc_id,
                    "selected_node_ids": list(route.selected_node_ids),
                    "section_ids": list(route.section_ids),
                    "rollup_ids": list(route.rollup_ids),
                    "parent_count": len(route.parent_ids),
                }
                for route in routes
            ]
            for lane_id, routes in output.items()
        }
        diagnostics["routed_document_count"] = sum(
            len(routes) for routes in output.values()
        )
        diagnostics["parent_id_count"] = sum(
            len(route.parent_ids) for routes in output.values() for route in routes
        )
        return output, diagnostics


summary_tree_navigator = SummaryTreeNavigator()

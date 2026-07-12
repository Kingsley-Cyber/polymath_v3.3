"""Top-down document routing over durable document-summary cards.

Routing cards select candidate documents; they are never returned as answer
evidence. Child/parent retrieval must still produce the supporting passages.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from config import get_settings
from services.ingestion.tier0 import SHARED_DOCSUM

logger = logging.getLogger(__name__)

_TECHNICAL_REPORT_RE = re.compile(
    r"\b(?:backfill|repair|migration|append|ingest(?:ion)?|pipeline)\b.*\breport\b|"
    r"\bstatus\s+report\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DocumentRoute:
    lane_id: str
    corpus_id: str
    doc_id: str
    score: float
    title: str = ""
    summary: str = ""
    concepts: tuple[str, ...] = ()
    section_ids: tuple[str, ...] = ()


def merge_grounded_document_route_hints(
    routes: dict[str, list[DocumentRoute]],
    route_hints: dict[str, list[dict[str, Any]]],
    *,
    max_per_lane: int = 6,
) -> tuple[dict[str, list[DocumentRoute]], dict[str, list[dict[str, Any]]]]:
    """Reserve provenance-backed documents before filling semantic slots."""

    merged = {lane_id: list(values) for lane_id, values in routes.items()}
    applied: dict[str, list[dict[str, Any]]] = {}
    for lane_id, hints in route_hints.items():
        grounded_routes = [
            DocumentRoute(
                lane_id=lane_id,
                corpus_id=str(hint.get("corpus_id") or ""),
                doc_id=str(hint.get("doc_id") or ""),
                score=float(hint.get("score") or 0.0),
                title=str(hint.get("title") or ""),
                summary=str(hint.get("summary") or ""),
                concepts=tuple(
                    str(value)
                    for value in (hint.get("concepts") or [])
                    if str(value)
                ),
                section_ids=tuple(
                    str(value)
                    for value in (hint.get("section_ids") or [])
                    if str(value)
                ),
            )
            for hint in hints
            if hint.get("corpus_id") and hint.get("doc_id")
        ]
        if not grounded_routes:
            continue
        existing = list(merged.get(lane_id) or [])
        grounded_keys = {
            (route.corpus_id, route.doc_id) for route in grounded_routes
        }
        by_document = {
            (route.corpus_id, route.doc_id): route for route in grounded_routes
        }
        for route in existing:
            key = (route.corpus_id, route.doc_id)
            current = by_document.get(key)
            if current is None or route.score > current.score:
                by_document[key] = route
        anchors = sorted(
            (
                route
                for key, route in by_document.items()
                if key in grounded_keys
            ),
            key=lambda route: (-route.score, route.corpus_id, route.doc_id),
        )
        remainder = diversify_document_routes(
            [
                route
                for key, route in by_document.items()
                if key not in grounded_keys
            ]
        )
        merged[lane_id] = (anchors + remainder)[: max(1, int(max_per_lane))]
        applied[lane_id] = list(hints)
    return merged, applied


def diversify_document_routes(
    routes: list[DocumentRoute],
    *,
    relevance_weight: float = 0.82,
) -> list[DocumentRoute]:
    """Order a relevant neighborhood by relevance plus profile novelty.

    Adaptive selection decides which documents are relevant. This second pass
    does not drop any of them; it only prevents near-duplicate profiles from
    occupying every early descent/reservation slot.
    """

    remaining = sorted(
        routes, key=lambda item: (-item.score, item.corpus_id, item.doc_id)
    )
    if len(remaining) <= 2:
        return remaining

    def terms(route: DocumentRoute) -> set[str]:
        return {
            value
            for value in re.findall(
                r"[a-z0-9]+",
                " ".join((route.title, route.summary, *route.concepts)).lower(),
            )
            if len(value) >= 3
        }

    token_sets = {(route.corpus_id, route.doc_id): terms(route) for route in remaining}
    selected = [remaining.pop(0)]
    while remaining:

        def objective(route: DocumentRoute) -> tuple[float, float, str, str]:
            current = token_sets[(route.corpus_id, route.doc_id)]
            max_overlap = 0.0
            for prior in selected:
                previous = token_sets[(prior.corpus_id, prior.doc_id)]
                union = current | previous
                overlap = len(current & previous) / len(union) if union else 0.0
                max_overlap = max(max_overlap, overlap)
            value = (
                relevance_weight * route.score - (1.0 - relevance_weight) * max_overlap
            )
            return value, route.score, route.corpus_id, route.doc_id

        next_route = max(remaining, key=objective)
        selected.append(next_route)
        remaining.remove(next_route)
    return selected


def _is_technical_report_route(route: DocumentRoute) -> bool:
    return bool(_TECHNICAL_REPORT_RE.search(route.title or ""))


def select_adaptive_routes(
    routes: list[DocumentRoute],
    *,
    min_score: float = 0.30,
    relative_margin: float = 0.20,
    min_keep: int = 1,
    max_keep: int = 6,
    cliff_min_gap: float = 0.08,
) -> list[DocumentRoute]:
    """Keep the coherent high-relevance document neighborhood for one lane.

    The router over-fetches first, applies a global lane-relative floor, then
    cuts at a meaningful score cliff. This avoids both a fixed top-3 truncation
    and the opposite failure of admitting a flat background-similarity tail.
    """

    ordered = sorted(
        routes, key=lambda item: (-item.score, item.corpus_id, item.doc_id)
    )
    if not ordered:
        return []
    top_score = float(ordered[0].score)
    floor = max(float(min_score), top_score - float(relative_margin))
    eligible = [route for route in ordered if float(route.score) >= floor]
    if not eligible:
        return []

    limit = min(max(1, int(max_keep)), len(eligible))
    eligible = eligible[:limit]
    minimum = min(max(1, int(min_keep)), len(eligible))
    gaps = [
        float(eligible[index].score) - float(eligible[index + 1].score)
        for index in range(len(eligible) - 1)
    ]
    meaningful = [
        (gap, index + 1)
        for index, gap in enumerate(gaps)
        if index + 1 >= minimum and gap >= float(cliff_min_gap)
    ]
    if meaningful:
        _gap, cut = max(meaningful, key=lambda item: (item[0], -item[1]))
        eligible = eligible[:cut]
    return eligible


def select_title_aligned_routes(
    routes: list[DocumentRoute],
    title_terms: tuple[str, ...],
    *,
    confidence_margin: float = 0.10,
) -> list[DocumentRoute]:
    """Gate explicit answer-object routes when document titles confirm scope.

    A title gate is applied only when its best match is close to the strongest
    semantic route. This preserves semantic fallback for corpora whose useful
    documents have opaque titles while preventing a generic support document
    from outranking an explicitly named list/book/tool document.
    """

    ordered = sorted(
        routes, key=lambda item: (-item.score, item.corpus_id, item.doc_id)
    )
    if not ordered or not title_terms:
        return ordered

    wanted = {
        token[:-1] if token.endswith("s") and len(token) > 3 else token
        for term in title_terms
        for token in re.findall(r"[a-z0-9]+", str(term).lower())
        if len(token) >= 3
    }

    def aligned(route: DocumentRoute) -> bool:
        title_tokens = {
            token[:-1] if token.endswith("s") and len(token) > 3 else token
            for token in re.findall(r"[a-z0-9]+", route.title.lower())
        }
        return bool(wanted & title_tokens)

    matches = [route for route in ordered if aligned(route)]
    if not matches:
        return ordered
    if float(matches[0].score) < float(ordered[0].score) - float(confidence_margin):
        return ordered
    return matches


class Tier0DocumentRouter:
    def __init__(self) -> None:
        settings = get_settings()
        self.client = AsyncQdrantClient(
            url=settings.QDRANT_URL,
            timeout=settings.QDRANT_TIMEOUT_SECONDS,
            prefer_grpc=settings.QDRANT_PREFER_GRPC,
            grpc_port=settings.QDRANT_GRPC_PORT,
        )

    async def route_lanes(
        self,
        lane_vectors: dict[str, list[float] | None],
        corpus_ids: list[str] | None,
        *,
        per_lane_per_corpus: int = 12,
        min_score: float = 0.30,
        relative_margin: float = 0.20,
        max_per_lane: int = 6,
        cliff_min_gap: float = 0.08,
        title_terms_by_lane: dict[str, tuple[str, ...]] | None = None,
    ) -> tuple[dict[str, list[DocumentRoute]], dict[str, object]]:
        """Route each semantic lane fairly across every selected corpus."""

        scoped_corpora = [str(value) for value in (corpus_ids or []) if str(value)]
        usable = {
            str(lane_id): vector
            for lane_id, vector in lane_vectors.items()
            if vector is not None
        }
        diagnostics: dict[str, object] = {
            "enabled": True,
            "collection": SHARED_DOCSUM,
            "lane_count": len(usable),
            "corpus_count": len(scoped_corpora),
            "routes": {},
            "failures": [],
        }
        if not usable or not scoped_corpora:
            diagnostics["reason"] = "missing_vectors_or_corpus_scope"
            return {}, diagnostics

        async def _one(lane_id: str, vector: list[float], corpus_id: str):
            query_filter = models.Filter(
                must=[
                    models.FieldCondition(
                        key="corpus_id",
                        match=models.MatchValue(value=corpus_id),
                    )
                ]
            )
            response = await self.client.query_points(
                collection_name=SHARED_DOCSUM,
                query=vector,
                using="dense",
                query_filter=query_filter,
                limit=max(1, int(per_lane_per_corpus)),
                with_payload=True,
            )
            return lane_id, corpus_id, list(response.points or [])

        tasks = [
            _one(lane_id, vector, corpus_id)
            for lane_id, vector in usable.items()
            for corpus_id in scoped_corpora
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        candidates: dict[str, list[DocumentRoute]] = {lane_id: [] for lane_id in usable}
        for raw in raw_results:
            if isinstance(raw, BaseException):
                cast_failures = diagnostics["failures"]
                if isinstance(cast_failures, list):
                    cast_failures.append(f"{type(raw).__name__}: {raw}"[:240])
                continue
            lane_id, corpus_id, hits = raw
            for hit in hits:
                score = float(hit.score or 0.0)
                payload = hit.payload or {}
                doc_id = str(payload.get("doc_id") or "")
                if not doc_id:
                    continue
                candidates[lane_id].append(
                    DocumentRoute(
                        lane_id=lane_id,
                        corpus_id=str(payload.get("corpus_id") or corpus_id),
                        doc_id=doc_id,
                        score=score,
                        title=str(payload.get("title") or ""),
                        summary=str(payload.get("summary") or ""),
                        concepts=tuple(
                            str(value)
                            for value in (payload.get("concepts") or [])
                            if str(value)
                        ),
                        section_ids=tuple(
                            str(value)
                            for value in (payload.get("section_ids") or [])
                            if str(value)
                        ),
                    )
                )

        routes: dict[str, list[DocumentRoute]] = {}
        candidate_counts: dict[str, int] = {}
        title_gates: dict[str, dict[str, object]] = {}
        for lane_id, values in candidates.items():
            deduped: dict[tuple[str, str], DocumentRoute] = {}
            for value in sorted(values, key=lambda item: -item.score):
                deduped.setdefault((value.corpus_id, value.doc_id), value)
            if not any(
                marker in lane_id.lower()
                for marker in ("backfill", "repair", "migration", "status_report")
            ):
                content_routes = {
                    key: route
                    for key, route in deduped.items()
                    if not _is_technical_report_route(route)
                }
                if content_routes:
                    deduped = content_routes
            candidate_counts[lane_id] = len(deduped)
            title_terms = tuple((title_terms_by_lane or {}).get(lane_id) or ())
            grouped: dict[str, list[DocumentRoute]] = {}
            for route in deduped.values():
                grouped.setdefault(route.corpus_id, []).append(route)
            per_corpus_max = max(
                2,
                math.ceil(max(1, int(max_per_lane)) / max(1, len(grouped))),
            )
            selected: list[DocumentRoute] = []
            title_before = 0
            title_after = 0
            for corpus_id in sorted(grouped):
                corpus_selected = select_adaptive_routes(
                    grouped[corpus_id],
                    min_score=min_score,
                    relative_margin=relative_margin,
                    max_keep=per_corpus_max,
                    cliff_min_gap=cliff_min_gap,
                )
                title_before += len(corpus_selected)
                corpus_selected = select_title_aligned_routes(
                    corpus_selected,
                    title_terms,
                )
                title_after += len(corpus_selected)
                selected.extend(corpus_selected)

            global_budget = max(max(1, int(max_per_lane)), len(grouped))
            if len(selected) > global_budget:
                anchors: list[DocumentRoute] = []
                for corpus_id in sorted(grouped):
                    corpus_routes = [
                        route for route in selected if route.corpus_id == corpus_id
                    ]
                    if corpus_routes:
                        anchors.append(
                            max(corpus_routes, key=lambda route: route.score)
                        )
                anchor_keys = {(route.corpus_id, route.doc_id) for route in anchors}
                remainder = diversify_document_routes(
                    [
                        route
                        for route in selected
                        if (route.corpus_id, route.doc_id) not in anchor_keys
                    ]
                )
                selected = anchors + remainder[: max(0, global_budget - len(anchors))]
            selected = diversify_document_routes(selected)
            routes[lane_id] = selected
            if title_terms:
                title_gates[lane_id] = {
                    "terms": list(title_terms),
                    "before": title_before,
                    "after": title_after,
                    "applied": title_after < title_before,
                }
        diagnostics["routes"] = {
            lane_id: [
                {
                    "corpus_id": route.corpus_id,
                    "doc_id": route.doc_id,
                    "score": round(route.score, 4),
                    "title": route.title,
                    "concepts": list(route.concepts),
                    "section_ids": list(route.section_ids),
                }
                for route in values
            ]
            for lane_id, values in routes.items()
        }
        diagnostics["routed_doc_count"] = len(
            {
                (route.corpus_id, route.doc_id)
                for values in routes.values()
                for route in values
            }
        )
        diagnostics["candidate_counts"] = candidate_counts
        diagnostics["title_gates"] = title_gates
        diagnostics["selection"] = {
            "per_lane_per_corpus_fetch": int(per_lane_per_corpus),
            "max_per_lane": int(max_per_lane),
            "min_score": float(min_score),
            "relative_margin": float(relative_margin),
            "cliff_min_gap": float(cliff_min_gap),
        }
        return routes, diagnostics


tier0_document_router = Tier0DocumentRouter()

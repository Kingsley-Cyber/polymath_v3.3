"""Top-down document routing over durable document-summary cards.

Routing cards select candidate documents; they are never returned as answer
evidence. Child/parent retrieval must still produce the supporting passages.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient, models

from config import get_settings
from services.ingestion.tier0 import SHARED_DOCSUM

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocumentRoute:
    lane_id: str
    corpus_id: str
    doc_id: str
    score: float
    title: str = ""


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
                    )
                )

        routes: dict[str, list[DocumentRoute]] = {}
        candidate_counts: dict[str, int] = {}
        for lane_id, values in candidates.items():
            deduped: dict[tuple[str, str], DocumentRoute] = {}
            for value in sorted(values, key=lambda item: -item.score):
                deduped.setdefault((value.corpus_id, value.doc_id), value)
            candidate_counts[lane_id] = len(deduped)
            routes[lane_id] = select_adaptive_routes(
                list(deduped.values()),
                min_score=min_score,
                relative_margin=relative_margin,
                max_keep=max_per_lane,
                cliff_min_gap=cliff_min_gap,
            )
        diagnostics["routes"] = {
            lane_id: [
                {
                    "corpus_id": route.corpus_id,
                    "doc_id": route.doc_id,
                    "score": round(route.score, 4),
                    "title": route.title,
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
        diagnostics["selection"] = {
            "per_lane_per_corpus_fetch": int(per_lane_per_corpus),
            "max_per_lane": int(max_per_lane),
            "min_score": float(min_score),
            "relative_margin": float(relative_margin),
            "cliff_min_gap": float(cliff_min_gap),
        }
        return routes, diagnostics


tier0_document_router = Tier0DocumentRouter()

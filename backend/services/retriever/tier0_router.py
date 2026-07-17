"""Top-down document routing over durable document-summary cards.

Routing cards select candidate documents; they are never returned as answer
evidence. Child/parent retrieval must still produce the supporting passages.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from config import get_settings
from services.ingestion.tier0 import SHARED_DOCSUM
from services.storage.mongo_contracts import restore_bson_utc_awareness

logger = logging.getLogger(__name__)

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REV_RE = re.compile(r"^rev:[0-9a-f]{64}$")
_MANIFEST_RE = re.compile(r"^projm:[0-9a-f]{64}$")
_DIGEST_AUTHORIZATION_TIMEOUT_SECONDS = 0.45
_DIGEST_CONTRACT_CACHE_MAX = 256
_DIGEST_CONTRACT_CACHE: dict[tuple[str, str, str], tuple[Any, Any]] = {}

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
    projection_role: str = ""
    projection_manifest_id: str = ""
    projection_parent_id: str = ""


def _digest_payload_has_complete_provenance(payload: dict[str, Any]) -> bool:
    required = (
        "corpus_id",
        "doc_id",
        "parent_id",
        "artifact_id",
        "artifact_revision_id",
        "projection_manifest_id",
        "projection_profile_hash",
        "source_cache_key",
        "source_job_id",
        "source_version_id",
        "schema_hash",
        "prompt_hash",
        "output_hash",
        "provenance_closure",
        "projected_payload_hash",
    )
    return bool(
        payload.get("chunk_type") == "semantic_digest"
        and payload.get("projection_role") == "semantic_digest"
        and all(payload.get(key) for key in required)
        and isinstance(payload.get("provenance_closure"), dict)
        and payload["provenance_closure"].get("mode")
        in {
            "job_prompt_version_labels_exact",
            "legacy_missing_job_prompt_version_labels",
        }
        and str(payload["provenance_closure"].get("job_id") or "")
        == str(payload.get("source_job_id") or "")
        and str(payload["provenance_closure"].get("cache_key") or "")
        == str(payload.get("source_cache_key") or "")
        and str(payload["provenance_closure"].get("adopted_prompt_version") or "")
        == str(payload.get("prompt_version") or "")
        and bool(
            str(
                payload["provenance_closure"].get("adopted_repair_prompt_version") or ""
            )
        )
        and _REV_RE.fullmatch(str(payload.get("artifact_revision_id") or ""))
        and _MANIFEST_RE.fullmatch(str(payload.get("projection_manifest_id") or ""))
        and all(
            _HASH_RE.fullmatch(str(payload.get(key) or ""))
            for key in (
                "projection_profile_hash",
                "schema_hash",
                "prompt_hash",
                "output_hash",
                "projected_payload_hash",
            )
        )
    )


async def _authorized_digest_point_ids(
    raw_results: list[Any],
) -> tuple[set[str], dict[str, int]]:
    """Validate current source lineage and exact manifest/profile compatibility."""

    from models.hash_taxonomy import namespace_hash
    from models.identifier_recipes import projection_point_id
    from models.projection_activation import ProjectionManifestV2, ProjectionOutboxV2
    from services.embedder import query_embedding_profile
    from services.ingestion.semantic_digest_claim_inputs import (
        document_source_version_id,
    )
    from services.semantic_activation import DIGEST_TIER0_PAYLOAD_SCHEMA_HASH
    from services.storage.record_status import with_active_records

    hits = [
        hit
        for raw in raw_results
        if not isinstance(raw, BaseException)
        for hit in raw[2]
        if (hit.payload or {}).get("chunk_type") == "semantic_digest"
    ]
    diagnostics: dict[str, Any] = {
        "seen": len(hits),
        "authorized": 0,
        "invalid_provenance": 0,
        "stale_lineage": 0,
        "profile_mismatch": 0,
        "missing_application_receipt": 0,
        "missing_manifest_contract": 0,
        "missing_outbox_contract": 0,
        "invalid_manifest_contract": 0,
        "invalid_outbox_contract": 0,
        "contract_cache_hits": 0,
        "contract_cache_misses": 0,
    }
    shaped = [
        hit
        for hit in hits
        if _digest_payload_has_complete_provenance(hit.payload or {})
    ]
    diagnostics["invalid_provenance"] = len(hits) - len(shaped)
    if not shaped:
        return set(), diagnostics
    try:
        from services.conversation import conversation_service

        db = conversation_service._db
        if db is None:
            diagnostics["profile_mismatch"] = len(shaped)
            return set(), diagnostics
        doc_terms = list(
            {
                (
                    str(hit.payload["corpus_id"]),
                    str(hit.payload["doc_id"]),
                )
                for hit in shaped
            }
        )
        corpus_ids = sorted({str(hit.payload["corpus_id"]) for hit in shaped})
        contract_keys = {
            str(hit.id): (
                str(hit.id),
                str(hit.payload["projection_manifest_id"]),
                str(hit.payload["projected_payload_hash"]),
            )
            for hit in shaped
        }
        contracts = {
            point_id: _DIGEST_CONTRACT_CACHE[key]
            for point_id, key in contract_keys.items()
            if key in _DIGEST_CONTRACT_CACHE
        }
        missing_point_ids = sorted(set(contract_keys) - set(contracts))
        missing_manifest_ids = sorted(
            {contract_keys[point_id][1] for point_id in missing_point_ids}
        )
        diagnostics["contract_cache_hits"] = len(contracts)
        diagnostics["contract_cache_misses"] = len(missing_point_ids)

        async def _load_missing_contract_rows():
            if not missing_point_ids:
                return [], []
            return await asyncio.gather(
                db["projection_manifests"]
                .find({"manifest_id": {"$in": missing_manifest_ids}}, {"_id": 0})
                .to_list(length=None),
                db["projection_outbox"]
                .find(
                    {
                        "schema_version": "projection_outbox.v2",
                        "point_id": {"$in": missing_point_ids},
                        "state": "applied",
                    },
                    {"_id": 0},
                )
                .to_list(length=None),
            )

        documents, corpora, contract_rows = await asyncio.gather(
            db["documents"]
            .find(
                with_active_records(
                    {
                        "$or": [
                            {"corpus_id": corpus_id, "doc_id": doc_id}
                            for corpus_id, doc_id in doc_terms
                        ]
                    }
                )
            )
            .to_list(length=None),
            db["corpora"]
            .find(
                with_active_records({"corpus_id": {"$in": corpus_ids}}),
                {"_id": 0, "corpus_id": 1, "default_ingestion_config": 1},
            )
            .to_list(length=None),
            _load_missing_contract_rows(),
        )
        manifest_rows, outbox_rows = contract_rows
    except Exception:
        diagnostics["profile_mismatch"] = len(shaped)
        return set(), diagnostics

    def _unique(rows, key_fn):
        grouped: dict[Any, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(key_fn(row), []).append(row)
        return {key: values[0] for key, values in grouped.items() if len(values) == 1}

    docs = _unique(
        documents,
        lambda row: (
            str(row.get("corpus_id") or ""),
            str(row.get("doc_id") or ""),
        ),
    )
    manifests = _unique(manifest_rows, lambda row: str(row.get("manifest_id") or ""))
    outboxes = _unique(outbox_rows, lambda row: str(row.get("point_id") or ""))
    corpus_rows = _unique(corpora, lambda row: str(row.get("corpus_id") or ""))
    for point_id in missing_point_ids:
        key = contract_keys[point_id]
        manifest_row = manifests.get(key[1])
        if manifest_row is None:
            diagnostics["missing_manifest_contract"] += 1
            continue
        outbox_row = outboxes.get(point_id)
        if outbox_row is None:
            diagnostics["missing_outbox_contract"] += 1
            continue
        try:
            manifest = ProjectionManifestV2.model_validate(
                restore_bson_utc_awareness(manifest_row)
            )
        except Exception as exc:
            diagnostics["invalid_manifest_contract"] += 1
            diagnostics.setdefault(
                "invalid_manifest_contract_error",
                f"{type(exc).__name__}: {exc}"[:240],
            )
            continue
        try:
            outbox = ProjectionOutboxV2.model_validate(
                restore_bson_utc_awareness(outbox_row)
            )
        except Exception as exc:
            diagnostics["invalid_outbox_contract"] += 1
            diagnostics.setdefault(
                "invalid_outbox_contract_error",
                f"{type(exc).__name__}: {exc}"[:240],
            )
            continue
        contract = (manifest, outbox)
        contracts[point_id] = contract
        if len(_DIGEST_CONTRACT_CACHE) >= _DIGEST_CONTRACT_CACHE_MAX:
            _DIGEST_CONTRACT_CACHE.pop(next(iter(_DIGEST_CONTRACT_CACHE)))
        _DIGEST_CONTRACT_CACHE[key] = contract
    settings = get_settings()
    allowed: set[str] = set()
    for hit in shaped:
        original_payload = dict(hit.payload or {})
        doc_key = (
            str(original_payload["corpus_id"]),
            str(original_payload["doc_id"]),
        )
        document = docs.get(doc_key)
        if document is None:
            diagnostics["stale_lineage"] += 1
            continue
        try:
            if document_source_version_id(document) != str(
                original_payload["source_version_id"]
            ):
                diagnostics["stale_lineage"] += 1
                continue
            manifest, outbox = contracts[str(hit.id)]
            corpus = corpus_rows[str(original_payload["corpus_id"])]
        except Exception:
            diagnostics["missing_application_receipt"] += 1
            continue
        cfg = dict(corpus.get("default_ingestion_config") or {})
        model_id = str(cfg.get("embedding_model_id") or settings.EMBEDDER_MODEL_NAME)
        model_revision = str(cfg.get("embedding_model_revision") or model_id)
        dims = int(cfg.get("embedding_dimension") or settings.EMBEDDING_DIMENSION)
        query_profile = query_embedding_profile(
            model_id=model_id,
            profile_name=str(
                cfg.get("query_instruction_profile")
                or settings.QWEN3_QUERY_INSTRUCTION_PROFILE
            ),
        )
        expected_quantization = (
            "binary" if settings.QDRANT_BINARY_QUANTIZATION_ENABLED else "float32"
        )
        profile_ok = bool(
            manifest.family == "document_summary"
            and manifest.representation_role == "semantic_digest"
            and manifest.target.collection_name == SHARED_DOCSUM
            and manifest.target.vector_name == "dense"
            and manifest.payload_schema_hash == DIGEST_TIER0_PAYLOAD_SCHEMA_HASH
            and manifest.projection_profile_hash
            == str(original_payload["projection_profile_hash"])
            and manifest.source_schema_hashes.get("semantic_digest.v1")
            == str(original_payload["schema_hash"])
            and manifest.embedding_profile.model_id == model_id
            and manifest.embedding_profile.model_revision == model_revision
            and manifest.embedding_profile.dims == dims
            and manifest.embedding_profile.quantization == expected_quantization
            and manifest.embedding_profile.instruction_version
            == str(query_profile["instruction_version"])
            and manifest.embedding_profile.document_side_instruction == "raw"
            and manifest.embedding_profile.sparse_recipe_version == "none"
            and manifest.search_compat.oversampling
            == float(settings.QDRANT_BINARY_QUANTIZATION_OVERSAMPLING)
            and manifest.search_compat.rescore_with_full_vectors
            == bool(settings.QDRANT_BINARY_QUANTIZATION_RESCORE)
            and manifest.search_compat.distance == "cosine"
        )
        payload_hash = str(original_payload.pop("projected_payload_hash"))
        point_ok = str(hit.id) == projection_point_id(
            str(original_payload["artifact_id"]),
            "semantic_digest",
            str(original_payload["projection_profile_hash"]),
        )
        if (
            not profile_ok
            or not point_ok
            or namespace_hash("body", original_payload) != payload_hash
        ):
            diagnostics["profile_mismatch"] += 1
            continue
        receipt = outbox.application_receipt
        application_ok = bool(
            outbox.manifest_id == manifest.manifest_id
            and outbox.point_id == str(hit.id)
            and outbox.projected_payload_hash == payload_hash
            and outbox.source.artifact_id == str(original_payload["artifact_id"])
            and outbox.source.corpus_id == str(original_payload["corpus_id"])
            and outbox.source.doc_id == str(original_payload["doc_id"])
            and outbox.source.parent_id == str(original_payload["parent_id"])
            and outbox.source.source_version_id
            == str(original_payload["source_version_id"])
            and outbox.source.source_id == str(original_payload["source_cache_key"])
            and outbox.source.ownership_id == str(original_payload["source_job_id"])
            and receipt is not None
            and receipt.target_collection == manifest.target.collection_name
            and receipt.vector_name == manifest.target.vector_name
            and receipt.point_id == str(hit.id)
            and receipt.projected_payload_hash == payload_hash
            and receipt.reconciled is True
        )
        if not application_ok:
            diagnostics["missing_application_receipt"] += 1
            continue
        allowed.add(str(hit.id))
    diagnostics["authorized"] = len(allowed)
    return allowed, diagnostics


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
                    str(value) for value in (hint.get("concepts") or []) if str(value)
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
        grounded_keys = {(route.corpus_id, route.doc_id) for route in grounded_routes}
        by_document = {
            (route.corpus_id, route.doc_id): route for route in grounded_routes
        }
        for route in existing:
            key = (route.corpus_id, route.doc_id)
            current = by_document.get(key)
            if current is None or route.score > current.score:
                by_document[key] = route
        anchors = sorted(
            (route for key, route in by_document.items() if key in grounded_keys),
            key=lambda route: (-route.score, route.corpus_id, route.doc_id),
        )
        remainder = diversify_document_routes(
            [route for key, route in by_document.items() if key not in grounded_keys]
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
        digest_enabled = bool(get_settings().SEMANTIC_DIGEST_TIER0_ENABLED)
        diagnostics: dict[str, object] = {
            "enabled": True,
            "collection": SHARED_DOCSUM,
            "lane_count": len(usable),
            "corpus_count": len(scoped_corpora),
            "routes": {},
            "failures": [],
            "semantic_digest_tier0_enabled": digest_enabled,
        }
        if not usable or not scoped_corpora:
            diagnostics["reason"] = "missing_vectors_or_corpus_scope"
            return {}, diagnostics

        from services.storage.qdrant_writer import binary_quantization_search_params

        quantization_params = binary_quantization_search_params()

        digest_fetch_limit = min(
            128,
            max(32, max(1, int(per_lane_per_corpus)) * 8),
        )

        async def _one(
            lane_id: str,
            vector: list[float],
            corpus_id: str,
            *,
            digest_only: bool,
        ):
            must = [
                models.FieldCondition(
                    key="corpus_id",
                    match=models.MatchValue(value=corpus_id),
                )
            ]
            if digest_only:
                must.append(
                    models.FieldCondition(
                        key="chunk_type",
                        match=models.MatchValue(value="semantic_digest"),
                    )
                )
            query_filter = models.Filter(
                must=must,
                # New digest points share the universal routing collection.
                # Explicit exclusion is required for a true dark launch;
                # filtering for legacy chunk_type would hide older cards that
                # predate the field.
                must_not=(
                    []
                    if digest_only
                    else [
                        models.FieldCondition(
                            key="chunk_type",
                            match=models.MatchValue(value="semantic_digest"),
                        )
                    ]
                ),
            )
            kwargs = {
                "collection_name": SHARED_DOCSUM,
                "query": vector,
                "using": "dense",
                "query_filter": query_filter,
                "limit": (
                    digest_fetch_limit
                    if digest_only
                    else max(1, int(per_lane_per_corpus))
                ),
                "with_payload": True,
            }
            if quantization_params is not None:
                kwargs["search_params"] = quantization_params
            response = await self.client.query_points(**kwargs)
            return lane_id, corpus_id, list(response.points or [])

        tasks = [
            _one(lane_id, vector, corpus_id, digest_only=digest_only)
            for lane_id, vector in usable.items()
            for corpus_id in scoped_corpora
            for digest_only in ((False, True) if digest_enabled else (False,))
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        if digest_enabled:
            authorization_started = perf_counter()
            try:
                authorized_digest_ids, digest_authorization = await asyncio.wait_for(
                    _authorized_digest_point_ids(raw_results),
                    timeout=_DIGEST_AUTHORIZATION_TIMEOUT_SECONDS,
                )
                digest_authorization["timed_out"] = 0
            except TimeoutError:
                authorized_digest_ids = set()
                digest_authorization = {
                    "seen": 0,
                    "authorized": 0,
                    "invalid_provenance": 0,
                    "stale_lineage": 0,
                    "profile_mismatch": 0,
                    "missing_application_receipt": 0,
                    "missing_manifest_contract": 0,
                    "missing_outbox_contract": 0,
                    "invalid_manifest_contract": 0,
                    "invalid_outbox_contract": 0,
                    "contract_cache_hits": 0,
                    "contract_cache_misses": 0,
                    "timed_out": 1,
                }
            digest_authorization["elapsed_ms"] = round(
                (perf_counter() - authorization_started) * 1000,
                3,
            )
        else:
            authorized_digest_ids = set()
            digest_authorization = {
                "seen": 0,
                "authorized": 0,
                "invalid_provenance": 0,
                "stale_lineage": 0,
                "profile_mismatch": 0,
                "missing_application_receipt": 0,
                "missing_manifest_contract": 0,
                "missing_outbox_contract": 0,
                "invalid_manifest_contract": 0,
                "invalid_outbox_contract": 0,
                "contract_cache_hits": 0,
                "contract_cache_misses": 0,
                "timed_out": 0,
                "elapsed_ms": 0.0,
            }
        diagnostics["semantic_digest_authorization"] = digest_authorization
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
                is_digest = payload.get("chunk_type") == "semantic_digest"
                if is_digest and (
                    not digest_enabled or str(hit.id) not in authorized_digest_ids
                ):
                    continue
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
                        projection_role=str(payload.get("projection_role") or ""),
                        projection_manifest_id=str(
                            payload.get("projection_manifest_id") or ""
                        ),
                        projection_parent_id=str(payload.get("parent_id") or ""),
                    )
                )

        routes: dict[str, list[DocumentRoute]] = {}
        candidate_counts: dict[str, int] = {}
        title_gates: dict[str, dict[str, object]] = {}
        for lane_id, values in candidates.items():
            deduped: dict[tuple[str, str], DocumentRoute] = {}
            for value in sorted(values, key=lambda item: -item.score):
                key = (value.corpus_id, value.doc_id)
                current = deduped.get(key)
                if current is None:
                    deduped[key] = value
                    continue
                if current.projection_role == "semantic_digest":
                    # Mutable display metadata is intentionally excluded from
                    # the immutable digest payload. Merge it from the legacy
                    # card when both route the same current document.
                    deduped[key] = replace(
                        current,
                        title=current.title or value.title,
                        concepts=current.concepts or value.concepts,
                        section_ids=current.section_ids or value.section_ids,
                    )
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
                    "projection_role": route.projection_role,
                    "projection_manifest_id": route.projection_manifest_id,
                    "projection_parent_id": route.projection_parent_id,
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
        diagnostics["semantic_digest_route_count"] = sum(
            bool(route.projection_role == "semantic_digest")
            for values in routes.values()
            for route in values
        )
        diagnostics["title_gates"] = title_gates
        diagnostics["selection"] = {
            "per_lane_per_corpus_fetch": int(per_lane_per_corpus),
            "semantic_digest_overfetch": (
                int(digest_fetch_limit) if digest_enabled else 0
            ),
            "qdrant_query_count": len(tasks),
            "max_per_lane": int(max_per_lane),
            "min_score": float(min_score),
            "relative_margin": float(relative_margin),
            "cliff_min_gap": float(cliff_min_gap),
        }
        return routes, diagnostics


tier0_document_router = Tier0DocumentRouter()

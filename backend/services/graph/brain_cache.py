"""Durable Brain View snapshot cache.

The interactive Brain View is expensive because the bridge pass walks from
Document anchors through high-mention entities into other Document anchors.
This module keeps a Mongo-backed, signature-checked snapshot of the response
shape returned by ``services.graph.queries.get_brain_view``.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any, Literal

from neo4j import AsyncDriver

logger = logging.getLogger(__name__)

CACHE_COLLECTION = "graph_brain_view_cache"
DEFAULT_BRAIN_VIEW_LIMIT = 2000
DEFAULT_BRAIN_VIEW_BRIDGE_ENTITY_CAP = 32
DEFAULT_MAX_CACHE_ENTRIES = 512
DEFAULT_STALE_RETENTION_DAYS = 14
_WARMUP_DEBOUNCE_SECONDS = 30.0
_PENDING_WARMUP_TASKS: dict[str, asyncio.Task[Any]] = {}

BrainDetail = Literal["anchors", "bridges"]


def normalize_corpus_ids(corpus_ids: list[str]) -> list[str]:
    return sorted({str(cid).strip() for cid in corpus_ids if str(cid).strip()})


def _selection_key(corpus_ids: list[str]) -> str:
    payload = "\n".join(normalize_corpus_ids(corpus_ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bridge_cap_for_cache(detail: BrainDetail, bridge_entity_cap: int) -> int:
    return 0 if detail == "anchors" else max(1, int(bridge_entity_cap))


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


async def compute_brain_view_selection_signature(
    db: Any,
    corpus_ids: list[str],
) -> tuple[str, dict[str, str]]:
    """Return a stable signature over the selected corpora's document state."""

    from services.graph.analytics import compute_corpus_change_signature

    signatures: dict[str, str] = {}
    parts: list[str] = []
    for cid in normalize_corpus_ids(corpus_ids):
        sig = await compute_corpus_change_signature(db, cid)
        signatures[cid] = sig
        parts.append(f"{cid}:{sig}")
    selection_signature = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return selection_signature, signatures


def _cache_query(
    corpus_ids: list[str],
    *,
    detail: BrainDetail,
    limit: int,
    bridge_entity_cap: int,
) -> dict[str, Any]:
    return {
        "cache_key": _selection_key(corpus_ids),
        "detail": detail,
        "limit": int(limit),
        "bridge_entity_cap": _bridge_cap_for_cache(detail, bridge_entity_cap),
    }


def _annotate_cache_meta(
    payload: dict[str, Any],
    *,
    status: str,
    selection_signature: str,
    corpus_signatures: dict[str, str],
    built_at: Any = None,
) -> dict[str, Any]:
    annotated = copy.deepcopy(payload)
    meta = annotated.setdefault("meta", {})
    built = built_at
    if isinstance(built, datetime):
        built = built.isoformat()
    meta["brain_cache"] = {
        "status": status,
        "selection_signature": selection_signature,
        "corpus_signatures": corpus_signatures,
        "built_at": built,
    }
    return annotated


async def get_cached_brain_view(
    db: Any,
    corpus_ids: list[str],
    *,
    detail: BrainDetail,
    limit: int,
    bridge_entity_cap: int,
) -> tuple[dict[str, Any] | None, str, dict[str, str]]:
    selection_signature, corpus_signatures = await compute_brain_view_selection_signature(
        db, corpus_ids
    )
    cached = await db[CACHE_COLLECTION].find_one(
        _cache_query(
            corpus_ids,
            detail=detail,
            limit=limit,
            bridge_entity_cap=bridge_entity_cap,
        ),
        {"_id": 0},
    )
    if (
        not cached
        or cached.get("status") != "ready"
        or cached.get("selection_signature") != selection_signature
    ):
        return None, selection_signature, corpus_signatures

    payload = cached.get("payload") or {}
    return (
        _annotate_cache_meta(
            payload,
            status="hit",
            selection_signature=selection_signature,
            corpus_signatures=corpus_signatures,
            built_at=cached.get("computed_at"),
        ),
        selection_signature,
        corpus_signatures,
    )


async def store_brain_view_cache(
    db: Any,
    corpus_ids: list[str],
    payload: dict[str, Any],
    *,
    detail: BrainDetail,
    limit: int,
    bridge_entity_cap: int,
    selection_signature: str | None = None,
    corpus_signatures: dict[str, str] | None = None,
) -> None:
    if selection_signature is None or corpus_signatures is None:
        selection_signature, corpus_signatures = await compute_brain_view_selection_signature(
            db, corpus_ids
        )
    now = datetime.utcnow()
    normalized_ids = normalize_corpus_ids(corpus_ids)
    await db[CACHE_COLLECTION].update_one(
        _cache_query(
            normalized_ids,
            detail=detail,
            limit=limit,
            bridge_entity_cap=bridge_entity_cap,
        ),
        {
            "$set": {
                **_cache_query(
                    normalized_ids,
                    detail=detail,
                    limit=limit,
                    bridge_entity_cap=bridge_entity_cap,
                ),
                "corpus_ids": normalized_ids,
                "selection_signature": selection_signature,
                "corpus_signatures": corpus_signatures,
                "payload": copy.deepcopy(payload),
                "total_documents": (payload.get("meta") or {}).get("total_documents"),
                "total_bridges": (payload.get("meta") or {}).get("total_bridges"),
                "status": "ready",
                "computed_at": now,
                "updated_at": now,
            },
            "$unset": {"stale_at": ""},
        },
        upsert=True,
    )


async def get_brain_view_cache_status(
    db: Any,
    corpus_ids: list[str],
    *,
    detail: BrainDetail,
    limit: int,
    bridge_entity_cap: int,
) -> dict[str, Any]:
    """Inspect cache readiness for a selected Brain View without rebuilding."""

    normalized_ids = normalize_corpus_ids(corpus_ids)
    if not normalized_ids:
        return {
            "status": "empty",
            "corpus_ids": [],
            "detail": detail,
            "limit": int(limit),
            "bridge_entity_cap": _bridge_cap_for_cache(detail, bridge_entity_cap),
        }

    selection_signature, corpus_signatures = await compute_brain_view_selection_signature(
        db, normalized_ids
    )
    query = _cache_query(
        normalized_ids,
        detail=detail,
        limit=limit,
        bridge_entity_cap=bridge_entity_cap,
    )
    cached = await db[CACHE_COLLECTION].find_one(query, {"_id": 0, "payload": 0})
    if not cached:
        return {
            "status": "missing",
            **query,
            "corpus_ids": normalized_ids,
            "selection_signature": selection_signature,
            "corpus_signatures": corpus_signatures,
            "cached_signature": None,
            "built_at": None,
            "stale_at": None,
            "updated_at": None,
            "total_documents": None,
            "total_bridges": None,
        }

    cached_signature = cached.get("selection_signature")
    stored_status = str(cached.get("status") or "missing")
    status = (
        "ready"
        if stored_status == "ready" and cached_signature == selection_signature
        else "stale"
    )
    return {
        "status": status,
        **query,
        "corpus_ids": normalized_ids,
        "selection_signature": selection_signature,
        "corpus_signatures": corpus_signatures,
        "cached_signature": cached_signature,
        "stored_status": stored_status,
        "built_at": _iso_or_none(cached.get("computed_at")),
        "stale_at": _iso_or_none(cached.get("stale_at")),
        "updated_at": _iso_or_none(cached.get("updated_at")),
        "total_documents": cached.get("total_documents"),
        "total_bridges": cached.get("total_bridges"),
    }


async def prune_brain_view_cache(
    db: Any,
    *,
    max_entries: int = DEFAULT_MAX_CACHE_ENTRIES,
    stale_retention_days: int = DEFAULT_STALE_RETENTION_DAYS,
) -> dict[str, int]:
    """Best-effort cache cleanup for stale rows and old selection combos."""

    collection = db[CACHE_COLLECTION]
    cutoff = datetime.utcnow() - timedelta(days=max(1, int(stale_retention_days)))
    stale_result = await collection.delete_many(
        {
            "$or": [
                {"stale_at": {"$lt": cutoff}},
                {"status": {"$ne": "ready"}, "updated_at": {"$lt": cutoff}},
            ]
        }
    )
    deleted_stale = int(getattr(stale_result, "deleted_count", 0) or 0)

    try:
        total = int(await collection.count_documents({}))
    except Exception:
        total = 0
    overflow = max(0, total - max(1, int(max_entries)))
    deleted_overflow = 0
    if overflow:
        cursor = collection.find({}, {"_id": 1}).sort("updated_at", 1).limit(overflow)
        rows = await cursor.to_list(length=overflow)
        ids = [row.get("_id") for row in rows if row.get("_id") is not None]
        if ids:
            overflow_result = await collection.delete_many({"_id": {"$in": ids}})
            deleted_overflow = int(getattr(overflow_result, "deleted_count", 0) or 0)

    remaining = max(0, total - deleted_overflow)
    return {
        "deleted_stale": deleted_stale,
        "deleted_overflow": deleted_overflow,
        "remaining": remaining,
    }


async def get_or_build_brain_view(
    *,
    db: Any,
    driver: AsyncDriver,
    corpus_ids: list[str],
    detail: BrainDetail = "bridges",
    limit: int = DEFAULT_BRAIN_VIEW_LIMIT,
    bridge_entity_cap: int = DEFAULT_BRAIN_VIEW_BRIDGE_ENTITY_CAP,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Read through the durable cache, computing and storing on a miss."""

    normalized_ids = normalize_corpus_ids(corpus_ids)
    if not normalized_ids:
        return {
            "documents": [],
            "bridges": [],
            "meta": {
                "corpus_count": 0,
                "total_documents": 0,
                "total_bridges": 0,
                "limit_applied": limit,
                "brain_cache": {"status": "empty"},
            },
        }

    selection_signature = ""
    corpus_signatures: dict[str, str] = {}
    if not force_refresh:
        cached, selection_signature, corpus_signatures = await get_cached_brain_view(
            db,
            normalized_ids,
            detail=detail,
            limit=limit,
            bridge_entity_cap=bridge_entity_cap,
        )
        if cached is not None:
            logger.debug(
                "Brain View cache HIT corpora=%s detail=%s sig=%s",
                ",".join(cid[:8] for cid in normalized_ids),
                detail,
                selection_signature[:8],
            )
            return cached
    if not selection_signature:
        selection_signature, corpus_signatures = await compute_brain_view_selection_signature(
            db, normalized_ids
        )

    from services.graph.queries import get_brain_view

    logger.info(
        "Brain View cache MISS corpora=%s detail=%s sig=%s",
        ",".join(cid[:8] for cid in normalized_ids),
        detail,
        selection_signature[:8],
    )
    payload = await get_brain_view(
        driver,
        normalized_ids,
        limit=limit,
        bridge_entity_cap=bridge_entity_cap,
        detail=detail,
    )
    if not payload.get("_error"):
        await store_brain_view_cache(
            db,
            normalized_ids,
            payload,
            detail=detail,
            limit=limit,
            bridge_entity_cap=bridge_entity_cap,
            selection_signature=selection_signature,
            corpus_signatures=corpus_signatures,
        )
        try:
            await prune_brain_view_cache(db)
        except Exception as exc:
            logger.debug("Brain View cache prune skipped: %s", exc)
    return _annotate_cache_meta(
        payload,
        status="miss_stored" if not payload.get("_error") else "miss_error",
        selection_signature=selection_signature,
        corpus_signatures=corpus_signatures,
    )


async def invalidate_brain_view_cache_for_corpus(
    db: Any,
    corpus_id: str,
    *,
    delete: bool = False,
) -> int:
    """Mark or remove Brain View snapshots that include ``corpus_id``."""

    if delete:
        result = await db[CACHE_COLLECTION].delete_many({"corpus_ids": corpus_id})
        return int(getattr(result, "deleted_count", 0) or 0)
    now = datetime.utcnow()
    result = await db[CACHE_COLLECTION].update_many(
        {"corpus_ids": corpus_id},
        {"$set": {"status": "stale", "stale_at": now, "updated_at": now}},
    )
    return int(getattr(result, "modified_count", 0) or 0)


def schedule_brain_view_warmup_after_ingest(
    *,
    neo4j_driver: Any,
    db: Any,
    corpus_id: str,
    debounce_seconds: float = _WARMUP_DEBOUNCE_SECONDS,
) -> None:
    """Debounced single-corpus Brain View cache warmup after ingest."""

    if db is None or neo4j_driver is None:
        return

    existing = _PENDING_WARMUP_TASKS.get(corpus_id)
    if existing is not None and not existing.done():
        existing.cancel()

    async def _delayed_warmup() -> None:
        try:
            await asyncio.sleep(debounce_seconds)
        except asyncio.CancelledError:
            return
        try:
            await get_or_build_brain_view(
                db=db,
                driver=neo4j_driver,
                corpus_ids=[corpus_id],
                detail="anchors",
                limit=DEFAULT_BRAIN_VIEW_LIMIT,
                bridge_entity_cap=DEFAULT_BRAIN_VIEW_BRIDGE_ENTITY_CAP,
                force_refresh=True,
            )
            await get_or_build_brain_view(
                db=db,
                driver=neo4j_driver,
                corpus_ids=[corpus_id],
                detail="bridges",
                limit=DEFAULT_BRAIN_VIEW_LIMIT,
                bridge_entity_cap=DEFAULT_BRAIN_VIEW_BRIDGE_ENTITY_CAP,
                force_refresh=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Brain View cache warmup failed corpus=%s: %s",
                corpus_id[:8],
                exc,
            )
        finally:
            _PENDING_WARMUP_TASKS.pop(corpus_id, None)

    try:
        _PENDING_WARMUP_TASKS[corpus_id] = asyncio.create_task(_delayed_warmup())
    except RuntimeError as exc:
        logger.warning(
            "Brain View cache warmup skipped corpus=%s: %s",
            corpus_id[:8],
            exc,
        )

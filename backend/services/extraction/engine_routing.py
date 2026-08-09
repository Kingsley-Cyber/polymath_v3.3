"""Runtime routing for the extraction engine (owner-ordered, 2026-08-09).

One Mongo control document decides which sidecar serves the encoder, so an
MCP agent can enable the LAN CUDA workstation to accelerate the Mac without
restarts or env edits. Determinism is enforced in layers:

  1. PINNED_MODEL — the frozen release identity (id, revision, weights
     sha256). A route is only valid if the target's /health reports these
     exact pins; same weights on every device.
  2. expected_release — the per-call handshake: the client refuses any
     inference response whose release differs from the routed one.
  3. qualified_releases — a release may serve PRODUCTION only after its
     burned-battery + digest comparison passed and it was recorded here.
     Qualification is an ops act (record_qualified_release), never an
     agent-side parameter.

Fail-safe: any routing failure (no Mongo, no doc, timeout) falls back to
the env/default chain, which is the MPS host sidecar. The route cache
refreshes every ~30s, so a flip takes effect within one window without
touching running items.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

ROUTING_COLLECTION = "extraction_engine_routing"
ROUTING_DOC_ID = "primary"

# Frozen release identity (release/extraction-v1.yaml). Every routable
# sidecar must serve exactly these weights.
PINNED_MODEL = {
    "id": "knowledgator/gliner-relex-large-v1.0",
    "revision": "4aedc9226a5ac9e2f6b5ea3e91c1ee577c88a290",
    "weights_sha256": "7c5bd751e1b24e4254d70fe4355a986cd65400676ce3735f7752429fcc26960a",
}

# The production baseline is qualified by definition — it is the release the
# whole semantic program was proven against.
BASELINE_QUALIFIED_RELEASES = ("relex-large-mps-sidecar-v1",)

_CACHE_TTL_SECONDS = 30.0
_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "route": None}


def _mongo_collection():
    uri = os.environ.get("MONGODB_URI", "").strip()
    if not uri:
        return None
    from pymongo import MongoClient

    client = MongoClient(uri, serverSelectionTimeoutMS=2000, connectTimeoutMS=2000)
    dbname = os.environ.get("MONGODB_DB", "polymath")
    return client[dbname][ROUTING_COLLECTION]


def active_route(*, force_refresh: bool = False) -> dict[str, Any] | None:
    """Return the routing document, TTL-cached; None means 'use env chain'."""
    now = time.monotonic()
    with _cache_lock:
        if not force_refresh and now - _cache["at"] < _CACHE_TTL_SECONDS:
            return _cache["route"]
    route = None
    try:
        collection = _mongo_collection()
        if collection is not None:
            route = collection.find_one({"_id": ROUTING_DOC_ID})
    except Exception:  # noqa: BLE001 — routing is fail-safe to env
        route = None
    with _cache_lock:
        _cache["at"] = time.monotonic()
        _cache["route"] = route
    return route


def routed_sidecar_url() -> str | None:
    route = active_route()
    url = (route or {}).get("sidecar_url")
    return str(url).rstrip("/") if url else None


def routed_expected_release() -> str | None:
    route = active_route()
    value = (route or {}).get("expected_release")
    return str(value) if value else None


def qualified_releases(route: dict[str, Any] | None = None) -> tuple[str, ...]:
    row = route if route is not None else active_route()
    extra = tuple(str(r) for r in (row or {}).get("qualified_releases") or ())
    return tuple(dict.fromkeys(BASELINE_QUALIFIED_RELEASES + extra))


async def write_route(
    db: Any,
    *,
    sidecar_url: str,
    expected_release: str,
    mode: str,
    updated_by: str,
    note: str = "",
) -> dict[str, Any]:
    """Persist the routing decision (async caller, e.g. the MCP tool)."""
    now = __import__("datetime").datetime.utcnow()
    doc = {
        "_id": ROUTING_DOC_ID,
        "schema_version": "polymath.engine_routing.v1",
        "sidecar_url": sidecar_url.rstrip("/"),
        "expected_release": expected_release,
        "mode": mode,
        "updated_by": updated_by,
        "note": note,
        "updated_at": now,
    }
    existing = await db[ROUTING_COLLECTION].find_one({"_id": ROUTING_DOC_ID}) or {}
    if existing.get("qualified_releases"):
        doc["qualified_releases"] = existing["qualified_releases"]
    if existing.get("wake"):
        doc["wake"] = existing["wake"]
    if existing.get("worker_stack"):
        doc["worker_stack"] = existing["worker_stack"]
    # Append-only audit trail (capped): every flip is reconstructible —
    # who routed where, when, in which mode, and what it replaced.
    await db[ROUTING_COLLECTION].update_one(
        {"_id": ROUTING_DOC_ID},
        {"$set": doc,
         "$push": {"route_history": {
             "$each": [{
                 "at": now, "by": updated_by, "mode": mode, "note": note,
                 "sidecar_url": doc["sidecar_url"],
                 "expected_release": expected_release,
                 "replaced_url": existing.get("sidecar_url"),
                 "replaced_release": existing.get("expected_release"),
             }],
             "$slice": -50,
         }}},
        upsert=True,
    )
    invalidate_cache()
    return doc


async def clear_route(db: Any) -> None:
    await db[ROUTING_COLLECTION].delete_one({"_id": ROUTING_DOC_ID})
    invalidate_cache()


async def record_qualified_release(db: Any, *, release: str, evidence: str) -> None:
    """Ops act: mark a release production-eligible after its battery passed."""
    await db[ROUTING_COLLECTION].update_one(
        {"_id": ROUTING_DOC_ID},
        {
            "$addToSet": {"qualified_releases": release},
            "$push": {"qualification_evidence": {
                "release": release, "evidence": evidence,
                "at": __import__("datetime").datetime.utcnow(),
            }},
        },
        upsert=True,
    )
    invalidate_cache()


def describe_route() -> dict[str, Any]:
    """Deterministic one-call summary of the control plane's routing state.

    source: which layer decides the engine right now — 'routing_doc' when the
    control document holds a URL, else 'env' (RELEX_SIDECAR_URL), else
    'default' (the MPS host chain). Safe under all failure modes.
    """
    route = active_route()
    url = (route or {}).get("sidecar_url")
    if url:
        source = "routing_doc"
    elif os.environ.get("RELEX_SIDECAR_URL", "").strip():
        source, url = "env", os.environ["RELEX_SIDECAR_URL"].strip()
    else:
        source, url = "default", None
    history = list((route or {}).get("route_history") or [])
    last = history[-1] if history else None
    return {
        "schema_version": (route or {}).get("schema_version"),
        "source": source,
        "sidecar_url": url,
        "mode": (route or {}).get("mode"),
        "expected_release": (route or {}).get("expected_release"),
        "qualified_releases": list(qualified_releases(route)),
        "wake_configured": bool(((route or {}).get("wake") or {}).get("mac_address")),
        "last_change": ({k: last.get(k) for k in ("at", "by", "mode", "note",
                         "sidecar_url", "replaced_url")} if last else None),
        "history_length": len(history),
    }


def invalidate_cache() -> None:
    with _cache_lock:
        _cache["at"] = 0.0
        _cache["route"] = None

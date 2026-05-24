"""User-scoped MCP API keys.

The original MCP sidecar supported one static `MCP_API_KEY` from `.env`. That
is useful for trusted automation, but it is system-scoped and requires service
restart to rotate. This module adds database-backed user keys that can be
generated from Settings, shown once, and validated by the MCP sidecar without
restarting containers.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from typing import Any
from uuid import uuid4


COLLECTION = "mcp_api_keys"
KEY_PREFIX = "pmcp_"


def _utcnow() -> datetime:
    return datetime.utcnow()


def generate_plaintext_key() -> str:
    """Return a bearer token safe to show once to the user."""
    return f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_mcp_key(token: str) -> str:
    """Stable one-way hash for lookup. Never store plaintext MCP keys."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def ensure_mcp_key_indexes(db: Any) -> None:
    """Create indexes used by validation and per-user listing.

    Idempotent; callers can run this before create/list/validate without
    coordinating a startup migration.
    """
    coll = db[COLLECTION]
    await coll.create_index("token_hash", unique=True, background=True)
    await coll.create_index([("user_id", 1), ("revoked_at", 1)], background=True)
    await coll.create_index("key_id", unique=True, background=True)


async def create_mcp_key(
    db: Any,
    *,
    user_id: str,
    name: str | None = None,
) -> dict[str, Any]:
    """Create a user-scoped MCP key and return plaintext once."""
    await ensure_mcp_key_indexes(db)
    token = generate_plaintext_key()
    now = _utcnow()
    key_id = f"mcp_{uuid4().hex[:16]}"
    display_name = (name or "").strip() or "MCP key"
    doc = {
        "key_id": key_id,
        "user_id": user_id,
        "name": display_name[:80],
        "token_hash": hash_mcp_key(token),
        "prefix": token[:14],
        "created_at": now,
        "last_used_at": None,
        "revoked_at": None,
    }
    await db[COLLECTION].insert_one(doc)
    return {
        "key_id": key_id,
        "name": doc["name"],
        "api_key": token,
        "prefix": doc["prefix"],
        "created_at": now.isoformat(),
        "restart_required": False,
        "scope": "user",
    }


def _public_key_doc(doc: dict[str, Any]) -> dict[str, Any]:
    def _iso(value: Any) -> str | None:
        return value.isoformat() if hasattr(value, "isoformat") else value

    return {
        "key_id": doc.get("key_id"),
        "name": doc.get("name"),
        "prefix": doc.get("prefix"),
        "created_at": _iso(doc.get("created_at")),
        "last_used_at": _iso(doc.get("last_used_at")),
        "revoked_at": _iso(doc.get("revoked_at")),
        "scope": "user",
    }


async def list_mcp_keys(db: Any, *, user_id: str) -> list[dict[str, Any]]:
    """List non-secret metadata for a user's active MCP keys."""
    await ensure_mcp_key_indexes(db)
    cursor = (
        db[COLLECTION]
        .find({"user_id": user_id, "revoked_at": None}, {"token_hash": 0, "_id": 0})
        .sort("created_at", -1)
    )
    return [_public_key_doc(doc) async for doc in cursor]


async def revoke_mcp_key(db: Any, *, user_id: str, key_id: str) -> bool:
    """Soft-revoke a key owned by the user."""
    await ensure_mcp_key_indexes(db)
    result = await db[COLLECTION].update_one(
        {"user_id": user_id, "key_id": key_id, "revoked_at": None},
        {"$set": {"revoked_at": _utcnow()}},
    )
    return bool(result.modified_count)


async def validate_user_mcp_key(db: Any, token: str | None) -> str | None:
    """Return the owning user_id when token is an active user MCP key."""
    if not token or not token.startswith(KEY_PREFIX):
        return None
    await ensure_mcp_key_indexes(db)
    digest = hash_mcp_key(token)
    doc = await db[COLLECTION].find_one(
        {"token_hash": digest, "revoked_at": None},
        {"user_id": 1},
    )
    if not doc or not doc.get("user_id"):
        return None
    await db[COLLECTION].update_one(
        {"token_hash": digest, "revoked_at": None},
        {"$set": {"last_used_at": _utcnow()}},
    )
    return str(doc["user_id"])

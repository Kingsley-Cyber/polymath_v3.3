"""
Phase F — User Query Preferences service.

One Mongo doc per user mapping the 3 query-time roles to entries in the
`model_pool` collection, plus per-user Ollama exclusions:

    {
      user_id: str (unique),
      hyde_pool_id: str | None,
      agentic_pool_id: str | None,
      query_pool_id: str | None,
      ollama_exclusions: list[str],
      updated_at: datetime,
    }

Chip storage lives in `model_pool` — this collection ONLY references
entry_ids. When a referenced chip is deleted, `cleanup_pool_id_refs` nulls
the dangling reference (called from `model_pool.delete`).
"""

from __future__ import annotations

import logging
from datetime import datetime

from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

COLLECTION = "user_query_preferences"
ROLE_FIELDS = ("hyde_pool_id", "agentic_pool_id", "query_pool_id")


class QueryPrefsService:
    """CRUD for the per-user query preferences doc."""

    def __init__(self) -> None:
        self._db: AsyncIOMotorDatabase | None = None

    def attach(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    @property
    def col(self):
        if self._db is None:
            raise RuntimeError("QueryPrefsService not attached to a DB")
        return self._db[COLLECTION]

    async def get(self, user_id: str) -> dict:
        """Fetch (or synthesize) the prefs doc for a user. Always returns a
        dict — never None — so callers don't need to None-check.
        """
        doc = await self.col.find_one({"user_id": user_id})
        if not doc:
            return {
                "user_id": user_id,
                "hyde_pool_id": None,
                "agentic_pool_id": None,
                "query_pool_id": None,
                "ollama_exclusions": [],
                "updated_at": None,
            }
        return {
            "user_id": doc["user_id"],
            "hyde_pool_id": doc.get("hyde_pool_id"),
            "agentic_pool_id": doc.get("agentic_pool_id"),
            "query_pool_id": doc.get("query_pool_id"),
            "ollama_exclusions": list(doc.get("ollama_exclusions") or []),
            "updated_at": doc.get("updated_at").isoformat() if doc.get("updated_at") else None,
        }

    async def upsert(self, user_id: str, patch: dict) -> dict:
        """Partial update. Keys absent from `patch` are preserved.

        Validates that any non-null *_pool_id refers to an entry that exists
        in model_pool for this user. Raises ValueError on unknown id.
        """
        from services.model_pool import model_pool_service

        # Validate referenced entry_ids
        owned_ids: set[str] | None = None
        for field in ROLE_FIELDS:
            if field in patch and patch[field] is not None:
                if owned_ids is None:
                    entries = await model_pool_service.list_for_user(user_id)
                    owned_ids = {e["entry_id"] for e in entries}
                if patch[field] not in owned_ids:
                    raise ValueError(
                        f"{field}={patch[field]!r} does not refer to a model_pool "
                        f"entry owned by user {user_id}"
                    )

        set_fields: dict = {"updated_at": datetime.utcnow()}
        for field in ROLE_FIELDS:
            if field in patch:
                set_fields[field] = patch[field]
        if "ollama_exclusions" in patch and patch["ollama_exclusions"] is not None:
            # Defensive: list of strings only.
            set_fields["ollama_exclusions"] = [
                str(x) for x in patch["ollama_exclusions"]
            ]

        await self.col.update_one(
            {"user_id": user_id},
            {"$set": set_fields, "$setOnInsert": {"user_id": user_id}},
            upsert=True,
        )
        return await self.get(user_id)

    async def cleanup_pool_id_refs(self, user_id: str, entry_id: str) -> None:
        """Called when a model_pool entry is deleted — null out any role
        slot that referenced it so resolution falls back cleanly.
        """
        update: dict = {}
        for field in ROLE_FIELDS:
            update[field] = None  # placeholder
        # Build a single $set that nulls only matching fields
        prefs = await self.col.find_one({"user_id": user_id})
        if not prefs:
            return
        to_unset: dict = {}
        for field in ROLE_FIELDS:
            if prefs.get(field) == entry_id:
                to_unset[field] = None
        if not to_unset:
            return
        to_unset["updated_at"] = datetime.utcnow()
        await self.col.update_one(
            {"user_id": user_id}, {"$set": to_unset}
        )
        logger.info(
            "Query prefs cleanup: nulled %s for user %s after pool delete %s",
            sorted(k for k in to_unset if k != "updated_at"),
            user_id,
            entry_id,
        )


query_prefs_service = QueryPrefsService()

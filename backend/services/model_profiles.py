"""
Phase 19.3 — Custom Model Profiles service

Per-user model profiles (Agent-Zero-style). A profile wraps:
    - label            : friendly name shown in the UI and dropdown
    - base_url         : OpenAI-compatible endpoint
    - model_name       : provider-side model id
    - api_key          : plaintext at create/update; Fernet-encrypted at rest
    - extra_params     : dict merged into the LiteLLM request body

Chat-time resolution:
    The chat orchestrator translates a model string of the shape
    `profile:<profile_id>` into base_url + api_key + model_name by calling
    `get_resolved()`. llm_service remains profile-agnostic — it just receives
    api_base / api_key / extra_params kwargs.

Security:
    - Keys are Fernet-encrypted in MongoDB (see services/secrets.py).
    - The masked form (`sk-****abc4`) is the only thing returned to the frontend.
    - Plaintext decryption happens only at call-time, never logged.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any
from uuid import uuid4

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from services.secrets import decrypt, encrypt, mask

logger = logging.getLogger(__name__)

COLLECTION = "model_profiles"


class ModelProfilesService:
    """Thin Mongo-backed CRUD for user-owned model profiles."""

    def __init__(self) -> None:
        self._db: AsyncIOMotorDatabase | None = None

    def attach(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    @property
    def col(self):
        if self._db is None:
            raise RuntimeError("ModelProfilesService not attached to a DB")
        return self._db[COLLECTION]

    # ── CRUD ───────────────────────────────────────────────────────────

    async def list_for_user(self, user_id: str) -> list[dict]:
        """Return every profile owned by user_id with api_keys MASKED."""
        cursor = self.col.find({"user_id": user_id}).sort("created_at", 1)
        out: list[dict] = []
        async for doc in cursor:
            out.append(self._to_public(doc))
        return out

    async def create(
        self,
        user_id: str,
        label: str,
        base_url: str,
        model_name: str,
        api_key: str,
        extra_params: dict | None = None,
    ) -> dict:
        now = datetime.utcnow()
        doc = {
            "profile_id": str(uuid4()),
            "user_id": user_id,
            "label": label,
            "base_url": base_url.rstrip("/"),
            "model_name": model_name,
            "api_key_enc": encrypt(api_key or ""),
            "extra_params": extra_params or {},
            "created_at": now,
            "updated_at": now,
        }
        await self.col.insert_one(doc)
        logger.info("Model profile created: user=%s profile=%s label=%r",
                    user_id, doc["profile_id"], label)
        return self._to_public(doc)

    async def update(
        self,
        user_id: str,
        profile_id: str,
        patch: dict,
    ) -> dict | None:
        """
        Partial update. Semantics:
          - api_key empty/None/absent  → leave existing ciphertext unchanged.
          - Any other field None/absent → leave unchanged.
        """
        set_fields: dict[str, Any] = {"updated_at": datetime.utcnow()}
        for key in ("label", "model_name", "extra_params"):
            if key in patch and patch[key] is not None:
                set_fields[key] = patch[key]
        if "base_url" in patch and patch["base_url"]:
            set_fields["base_url"] = patch["base_url"].rstrip("/")
        if "api_key" in patch and patch["api_key"]:
            set_fields["api_key_enc"] = encrypt(patch["api_key"])

        result = await self.col.find_one_and_update(
            {"profile_id": profile_id, "user_id": user_id},
            {"$set": set_fields},
            return_document=True,
        )
        if not result:
            return None
        return self._to_public(result)

    async def delete(self, user_id: str, profile_id: str) -> bool:
        r = await self.col.delete_one(
            {"profile_id": profile_id, "user_id": user_id}
        )
        return r.deleted_count == 1

    async def get_resolved(
        self, user_id: str, profile_id: str
    ) -> dict | None:
        """
        Fetch profile and DECRYPT api_key. Used by the chat orchestrator to
        translate `profile:<id>` into concrete creds before handing to LiteLLM.

        Returns None if the profile doesn't exist or doesn't belong to the user.
        """
        doc = await self.col.find_one(
            {"profile_id": profile_id, "user_id": user_id}
        )
        if not doc:
            return None
        plaintext_key = decrypt(doc.get("api_key_enc", ""))
        return {
            "profile_id": doc["profile_id"],
            "label": doc["label"],
            "base_url": doc["base_url"],
            "model_name": doc["model_name"],
            "api_key": plaintext_key,
            "extra_params": doc.get("extra_params", {}),
        }

    async def test_connection(
        self, user_id: str, profile_id: str
    ) -> dict[str, Any]:
        """
        Send a tiny `hi` to the profile's /chat/completions endpoint.
        Returns {ok, status?, latency_ms?, error?}.
        """
        resolved = await self.get_resolved(user_id, profile_id)
        if not resolved:
            return {"ok": False, "error": "Profile not found"}

        url = f"{resolved['base_url'].rstrip('/')}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if resolved["api_key"]:
            headers["Authorization"] = f"Bearer {resolved['api_key']}"
        body = {
            "model": resolved["model_name"],
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "stream": False,
        }

        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=body, headers=headers)
            latency_ms = int((time.monotonic() - started) * 1000)
            if resp.status_code >= 400:
                return {
                    "ok": False,
                    "status": resp.status_code,
                    "latency_ms": latency_ms,
                    "error": resp.text[:250],
                }
            return {
                "ok": True,
                "status": resp.status_code,
                "latency_ms": latency_ms,
            }
        except httpx.TimeoutException:
            return {"ok": False, "error": "Request timed out after 15s"}
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Test connection failed for profile=%s: %s", profile_id, exc)
            return {"ok": False, "error": str(exc)}

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _to_public(doc: dict) -> dict:
        """Build the masked shape the frontend gets."""
        cipher = doc.get("api_key_enc", "")
        plain = decrypt(cipher) if cipher else None
        return {
            "profile_id": doc["profile_id"],
            "label": doc["label"],
            "base_url": doc["base_url"],
            "model_name": doc["model_name"],
            "api_key_masked": mask(plain),
            "extra_params": doc.get("extra_params", {}),
            "created_at": doc["created_at"].isoformat()
            if doc.get("created_at")
            else None,
            "updated_at": doc["updated_at"].isoformat()
            if doc.get("updated_at")
            else None,
        }


model_profiles_service = ModelProfilesService()

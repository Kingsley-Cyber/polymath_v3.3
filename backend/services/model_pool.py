"""
Phase E — Unified Model Pool service.

One Mongo collection (`model_pool`) = every user-facing chat model.
Each row carries everything needed to route a request:

  {label, provider, base_url, model_name, api_key (Fernet), extra_params,
   context_length, enabled}

Key-storage model is HYBRID: an entry either carries its own api_key OR sets
`use_shared_key=True` to pull from the API Keys registry (`settings.api_keys`)
by `provider` name. This keeps the rotation story for "I have one Mistral key
for 10 models" simple without forcing self-contained duplication.

Chat-time resolution: chat_orchestrator detects `pool:<entry_id>` model
strings, calls `get_resolved()`, spreads base_url + decrypted key + extras
into the LiteLLM call (piggybacks on the Phase B profile pipeline).

Distinct from `model_profiles` (Phase B) — Phase E is the unified successor;
both remain concurrent for one release so old custom profiles still work.
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


# Pt10d — model_name validation registry.
#
# Two production failures handed us this regression guard:
#   1. settings.models.query_model_pool entry created with model_name=
#      "deepseek/admin" — the user mistakenly typed the pool name into
#      the model field. DeepSeek API returned 400 for every query
#      synthesis call until manually corrected in Mongo.
#   2. settings.models.query_model_pool entry created with model_name=
#      "deepseek/DeepSeek-V4-Flash" — title-case marketing name instead
#      of the API model id `deepseek-v4-flash`. Same 400 storm.
#
# Both look right at first glance ("admin" is a user/pool name; the
# DeepSeek brand IS spelled "DeepSeek V4 Flash"). The UI accepts any
# string. The provider rejects at request time, surfacing only as
# "synthesis model could not be reached" in the chat UI.
#
# The validator below catches the dominant failure classes BEFORE the
# entry is persisted. Per-provider allowlists are intentionally small
# and high-confidence — adding aliases later is cheap, and false
# negatives (a valid model rejected by validation) are recoverable via
# the `extra_params.skip_model_validation: true` escape hatch.
#
# Adding a new provider: extend MODEL_NAME_PATTERNS below with a regex
# that covers the provider's published model identifier scheme.

import re

# Known-good model name regexes per provider (lowercased match).
# Each pattern matches the part AFTER the `provider/` prefix that
# LiteLLM strips for routing. Pattern must be the FULL model id —
# anchored ^ to $.
MODEL_NAME_PATTERNS: dict[str, list[re.Pattern]] = {
    "deepseek": [
        # Production model ids as of 2026-05.
        re.compile(r"^deepseek/deepseek-(chat|reasoner|v4-flash|v4-pro|v3|coder)$"),
        re.compile(r"^deepseek-(chat|reasoner|v4-flash|v4-pro|v3|coder)$"),
    ],
    "openai": [
        # GPT family + o-series reasoning + embeddings.
        re.compile(r"^openai/(gpt-[34]|gpt-4o|gpt-4o-mini|o[134](-mini|-preview)?|text-embedding-3-(small|large))(-\w+)?$"),
        re.compile(r"^(gpt-[34]|gpt-4o|gpt-4o-mini|o[134](-mini|-preview)?)(-\w+)?$"),
    ],
    "anthropic": [
        re.compile(r"^anthropic/claude-(3|3-5|3-7|4|4-5|4-7)-(opus|sonnet|haiku)(-\d+)?(-\w+)?$"),
        re.compile(r"^claude-(3|3-5|3-7|4|4-5|4-7)-(opus|sonnet|haiku)(-\d+)?(-\w+)?$"),
    ],
    "mistral": [
        re.compile(r"^mistral/(mistral|codestral|magistral|open-mistral)-\S+$"),
    ],
    "siliconflow": [
        # Permissive — SiliconFlow hosts many third-party models with
        # vendor/model two-segment ids.
        re.compile(r"^openai/[\w\-/.]+/[\w\-/.]+$"),
    ],
}


class InvalidModelNameError(ValueError):
    """Raised when a model_name doesn't match any known-good pattern
    for its declared provider. Surfaced as HTTP 422 by the router."""


def validate_model_name(provider: str, model_name: str, *, allow_skip: bool = False) -> None:
    """Pt10d — reject obviously-wrong model_name values at save time.

    Catches the two dominant failure classes:
      - Pool/user/account name typed where the model id belongs
        ("admin", "default", "pool-1")
      - Marketing-style capitalization ("DeepSeek-V4-Flash") instead
        of API id ("deepseek-v4-flash")

    Bypass via `allow_skip=True` (router can flip this when the entry's
    extra_params carries `skip_model_validation: true`) so a genuinely
    novel model doesn't block configuration.

    Args:
        provider: lowercased provider key (deepseek, openai, etc.)
        model_name: the model identifier as stored (may include
            "provider/" prefix; pattern handles both forms)
        allow_skip: if True, validation is a no-op

    Raises:
        InvalidModelNameError: with a hint about the canonical form
    """
    if allow_skip:
        return
    name = (model_name or "").strip()
    if not name:
        raise InvalidModelNameError("model_name is required")
    # Catch the "user typed a pool/account name" failure class —
    # the dominant production failure observed twice on the same setup.
    obvious_typos = {"admin", "default", "user", "pool", "deepseek/admin"}
    if name.lower() in obvious_typos:
        raise InvalidModelNameError(
            f"model_name={name!r} looks like a pool/account name rather "
            f"than a model id. Set it to a real model — e.g. "
            f"deepseek/deepseek-v4-flash for DeepSeek."
        )
    # Catch the "title-case marketing name" failure class — model ids
    # at every major provider are lowercase. If the lowercased version
    # matches a pattern but the original doesn't, that's the bug.
    patterns = MODEL_NAME_PATTERNS.get((provider or "").lower(), [])
    if patterns:
        as_lower = name.lower()
        any_strict = any(p.match(name) for p in patterns)
        any_lower = any(p.match(as_lower) for p in patterns)
        if any_lower and not any_strict:
            raise InvalidModelNameError(
                f"model_name={name!r} has wrong capitalization for "
                f"provider={provider!r}. Provider model ids are "
                f"lowercase. Try {as_lower!r}."
            )
        # If neither matched, the name is genuinely unknown — fail.
        if not any_strict:
            raise InvalidModelNameError(
                f"model_name={name!r} is not a known {provider!r} model id. "
                f"Set extra_params.skip_model_validation=true to bypass "
                f"if this is a newly-released model not yet in the registry."
            )
    # Unknown provider — let it through (we don't enforce on providers
    # we haven't catalogued). The original 400 from the provider's API
    # is still the fallback safety net.

logger = logging.getLogger(__name__)

COLLECTION = "model_pool"


class ModelPoolService:
    """CRUD + resolution for the unified chat model pool."""

    def __init__(self) -> None:
        self._db: AsyncIOMotorDatabase | None = None

    def attach(self, db: AsyncIOMotorDatabase) -> None:
        self._db = db

    @property
    def col(self):
        if self._db is None:
            raise RuntimeError("ModelPoolService not attached to a DB")
        return self._db[COLLECTION]

    # ── CRUD ───────────────────────────────────────────────────────────

    async def list_for_user(self, user_id: str) -> list[dict]:
        """All pool entries for a user, masked."""
        cursor = self.col.find({"user_id": user_id}).sort("created_at", 1)
        return [self._to_public(doc) async for doc in cursor]

    async def create(
        self,
        user_id: str,
        label: str,
        provider: str,
        base_url: str,
        model_name: str,
        api_key: str | None = None,
        use_shared_key: bool = False,
        extra_params: dict | None = None,
        context_length: int | None = None,
        tags: list[str] | None = None,
        enabled: bool = True,
    ) -> dict:
        now = datetime.utcnow()
        # Pt10d — validate model_name before persistence. Save-time
        # validation prevents the deepseek/admin and
        # DeepSeek-V4-Flash failure classes that previously surfaced
        # only as runtime 400s during synthesis. Bypass available via
        # extra_params.skip_model_validation=true for novel models.
        _skip_validation = bool((extra_params or {}).get("skip_model_validation"))
        validate_model_name(
            provider=provider,
            model_name=model_name,
            allow_skip=_skip_validation,
        )
        # Persist api_key as ciphertext or empty if using shared
        api_key_enc = encrypt(api_key or "") if (api_key and not use_shared_key) else ""
        doc = {
            "entry_id": str(uuid4()),
            "user_id": user_id,
            "label": label,
            "provider": provider.lower().strip(),
            "base_url": base_url.rstrip("/"),
            "model_name": model_name,
            "api_key_enc": api_key_enc,
            "use_shared_key": bool(use_shared_key),
            "extra_params": extra_params or {},
            "context_length": context_length,
            "tags": tags or ["chat"],
            "enabled": bool(enabled),
            "created_at": now,
            "updated_at": now,
        }
        await self.col.insert_one(doc)
        logger.info(
            "Model pool entry created: user=%s entry=%s label=%r provider=%s",
            user_id, doc["entry_id"], label, provider,
        )
        return self._to_public(doc)

    async def update(
        self, user_id: str, entry_id: str, patch: dict,
    ) -> dict | None:
        # Pt10d — validate model_name on update too. If the patch is
        # changing provider OR model_name, re-check; otherwise the
        # stored values are presumed valid (they passed create-time
        # validation). Fetch current doc to fill in whichever field
        # the patch isn't touching.
        if "model_name" in patch or "provider" in patch:
            current = await self.col.find_one(
                {"user_id": user_id, "entry_id": entry_id},
                {"provider": 1, "model_name": 1, "extra_params": 1, "_id": 0},
            )
            if current is not None:
                effective_provider = (
                    patch.get("provider", current.get("provider")) or ""
                ).lower().strip()
                effective_model = patch.get("model_name", current.get("model_name", ""))
                effective_extras = patch.get("extra_params") or current.get("extra_params") or {}
                _skip_validation = bool(effective_extras.get("skip_model_validation"))
                validate_model_name(
                    provider=effective_provider,
                    model_name=effective_model,
                    allow_skip=_skip_validation,
                )
        set_fields: dict[str, Any] = {"updated_at": datetime.utcnow()}
        for key in (
            "label", "provider", "model_name", "extra_params",
            "context_length", "tags", "enabled", "use_shared_key",
        ):
            if key in patch and patch[key] is not None:
                value = patch[key]
                if key == "provider" and isinstance(value, str):
                    value = value.lower().strip()
                set_fields[key] = value
        if "base_url" in patch and patch["base_url"]:
            set_fields["base_url"] = patch["base_url"].rstrip("/")
        if "api_key" in patch and patch["api_key"]:
            # Plaintext only — skip masked sentinel
            if patch["api_key"] != "[set]":
                set_fields["api_key_enc"] = encrypt(patch["api_key"])
                set_fields["use_shared_key"] = False

        result = await self.col.find_one_and_update(
            {"entry_id": entry_id, "user_id": user_id},
            {"$set": set_fields},
            return_document=True,
        )
        return self._to_public(result) if result else None

    async def delete(self, user_id: str, entry_id: str) -> bool:
        r = await self.col.delete_one({"entry_id": entry_id, "user_id": user_id})
        if r.deleted_count == 1:
            # Phase F — null out any query_prefs role that referenced this chip
            # so resolution falls back cleanly instead of returning a dangling id.
            try:
                from services.query_prefs import query_prefs_service

                await query_prefs_service.cleanup_pool_id_refs(user_id, entry_id)
            except Exception as exc:
                logger.warning(
                    "query_prefs cleanup skipped after pool delete %s: %s",
                    entry_id, exc,
                )
        return r.deleted_count == 1

    async def get_resolved(
        self, user_id: str, entry_id: str
    ) -> dict | None:
        """
        Fetch a pool entry with DECRYPTED api_key, either from the entry
        itself or from the shared API Keys registry. Used by the chat
        orchestrator to translate `pool:<id>` → concrete LiteLLM creds.
        """
        doc = await self.col.find_one(
            {"entry_id": entry_id, "user_id": user_id}
        )
        if not doc:
            return None

        plaintext_key: str | None = None
        if doc.get("use_shared_key"):
            plaintext_key = await self._resolve_shared_key(user_id, doc["provider"])
        else:
            plaintext_key = decrypt(doc.get("api_key_enc", "") or "")

        return {
            "entry_id": doc["entry_id"],
            "label": doc["label"],
            "provider": doc["provider"],
            "base_url": doc["base_url"],
            "model_name": doc["model_name"],
            "api_key": plaintext_key or "",
            "extra_params": doc.get("extra_params", {}),
            "context_length": doc.get("context_length"),
            "enabled": doc.get("enabled", True),
        }

    async def test_connection(
        self, user_id: str, entry_id: str
    ) -> dict[str, Any]:
        """Fire a 1-token ping at the entry's base_url."""
        resolved = await self.get_resolved(user_id, entry_id)
        if not resolved:
            return {"ok": False, "error": "Entry not found"}

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
        except Exception as exc:
            logger.warning("Pool entry test failed: entry=%s err=%s", entry_id, exc)
            return {"ok": False, "error": str(exc)}

    # ── shared-key registry bridge ─────────────────────────────────────

    async def _resolve_shared_key(
        self, user_id: str, provider: str
    ) -> str | None:
        """Pull decrypted key from `settings.api_keys.<provider>` for this user."""
        if self._db is None:
            return None
        doc = await self._db["settings"].find_one(
            {"user_id": user_id}, projection={"api_keys": 1}
        )
        if not doc:
            return None
        stored = (doc.get("api_keys") or {}).get(provider)
        if not stored:
            return None
        return decrypt(stored)

    # ── migration ──────────────────────────────────────────────────────

    async def migrate_from_legacy(self, user_id: str) -> int:
        """
        One-shot import: existing Phase B `model_profiles` → pool entries.
        Runs only when the pool is empty for this user. Returns count imported.
        """
        if self._db is None:
            return 0
        if await self.col.count_documents({"user_id": user_id}, limit=1):
            return 0

        imported = 0
        cursor = self._db["model_profiles"].find({"user_id": user_id})
        async for profile in cursor:
            # Derive provider from the base_url — best-effort; user can fix later
            url = profile.get("base_url", "")
            provider = _guess_provider_from_url(url)
            await self.create(
                user_id=user_id,
                label=profile.get("label", "Migrated profile"),
                provider=provider,
                base_url=url,
                model_name=profile.get("model_name", ""),
                # api_key_enc is already Fernet; copy directly without re-encrypt
                api_key=None,
                use_shared_key=False,
                extra_params=profile.get("extra_params", {}),
            )
            # Overwrite the freshly-encrypted empty field with the original ciphertext
            await self.col.update_one(
                {"entry_id": {"$exists": True}, "label": profile.get("label", "")},
                {"$set": {"api_key_enc": profile.get("api_key_enc", "")}},
            )
            imported += 1

        if imported:
            logger.info(
                "Model pool migration: imported %d profile(s) for user=%s",
                imported, user_id,
            )
        return imported

    # ── helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _to_public(doc: dict) -> dict:
        """Masked view the frontend consumes."""
        if not doc:
            return {}
        cipher = doc.get("api_key_enc", "")
        plain = decrypt(cipher) if cipher else None
        return {
            "entry_id": doc["entry_id"],
            "label": doc["label"],
            "provider": doc.get("provider", ""),
            "base_url": doc["base_url"],
            "model_name": doc["model_name"],
            "api_key_masked": mask(plain) if not doc.get("use_shared_key") else "[shared]",
            "use_shared_key": bool(doc.get("use_shared_key")),
            "extra_params": doc.get("extra_params", {}),
            "context_length": doc.get("context_length"),
            "tags": doc.get("tags", ["chat"]),
            "enabled": doc.get("enabled", True),
            "created_at": doc["created_at"].isoformat() if doc.get("created_at") else None,
            "updated_at": doc["updated_at"].isoformat() if doc.get("updated_at") else None,
        }


def _guess_provider_from_url(url: str) -> str:
    """Best-effort provider inference from a base URL host."""
    url = (url or "").lower()
    if "openai" in url: return "openai"
    if "anthropic" in url: return "anthropic"
    if "deepseek" in url: return "deepseek"
    if "moonshot" in url: return "kimi"
    if "minimax" in url: return "minimax"
    if "mistral" in url: return "mistral"
    if "z.ai" in url: return "glm-coding"
    if "xiaomimimo" in url: return "mimo"
    if "groq" in url: return "groq"
    if "together" in url: return "together"
    if "openrouter" in url: return "openrouter"
    if "gemini" in url or "google" in url: return "gemini"
    if "ollama" in url or "localhost:11434" in url: return "ollama"
    return "custom"


model_pool_service = ModelPoolService()

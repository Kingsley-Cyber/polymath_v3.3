"""
Sprint 3 — Query model resolver.

Resolves three kinds of references at chat time:

  1. Explicit  `pool:<entry_id>` or `profile:<entry_id>` model strings
     (from a stored chat turn or per-request override).
  2. The user's HyDE default  (settings.models.hyde.pool_entry_id).
  3. The user's agentic/utility/reasoning defaults
     (settings.models.<role>.pool_entry_id).

Lookups prefer the new unified pool at `settings.models.query_model_pool`.
On miss, the resolver falls through to the legacy per-collection stores
(`model_pool`, `model_profiles`) so existing chat references and
pre-migration data keep working during the one-release deprecation window.

QUERY-TIME ONLY. Ingestion-time model resolution lives in
`services/ghost_a.py` and `services/ghost_b.py`.
"""

from __future__ import annotations

import logging
from typing import Literal

logger = logging.getLogger(__name__)

Kind = Literal["hyde", "agentic", "query", "reasoning", "utility"]

_KIND_TO_POOL_FIELD = {
    "hyde": "hyde",
    "agentic": "agentic",
    # Phase 24 — reasoning cascade analyst model
    "reasoning": "reasoning",
    "utility": "utility",
}


async def _shared_api_key_for_entry(
    user_id: str,
    entry: dict,
    model_name: str,
) -> str | None:
    """Return the user's shared key for a model-pool entry when the entry
    itself does not carry a per-entry key.

    The Models UI explicitly says blank per-entry keys fall back to the API
    Keys registry. Keep that promise here. Prefer the UI provider id
    (`mimo`, `siliconflow`, etc.) because OpenAI-compatible providers often
    store `model_name` as `openai/<model>` while their key is stored under the
    provider preset, not under `openai`.
    """

    from services.settings import settings_service

    keys = await settings_service.get_plaintext_keys_for_llm(user_id)
    provider = str(entry.get("provider") or "").strip().lower()
    if provider and keys.get(provider):
        return keys[provider]

    model_prefix = (model_name.split("/", 1)[0] if "/" in model_name else "").lower()
    if model_prefix and keys.get(model_prefix):
        return keys[model_prefix]

    return None


async def resolve_by_entry_id(
    user_id: str | None, entry_id: str
) -> dict | None:
    """Resolve a specific pool entry id → {model, api_base, api_key,
    extra_params}. Tries the unified pool first, then the legacy stores.
    Returns None if the entry can't be found anywhere.
    """
    if not user_id or not entry_id:
        return None

    # 1. Unified pool under settings.models
    from services.secrets import decrypt
    from services.settings import settings_service

    def _prefix(mname: str) -> str:
        """Pass-through for already-prefixed names; fall back to openai/
        for bare names that came from custom OpenAI-compatible endpoints."""
        mname = (mname or "").strip()
        return mname if "/" in mname else f"openai/{mname}"

    raw = await settings_service.get_models_raw(user_id)
    for entry in (raw.get("query_model_pool") or []):
        if not isinstance(entry, dict) or entry.get("entry_id") != entry_id:
            continue
        model_name = str(entry.get("model_name") or "").strip()
        ct = entry.get("api_key_ciphertext")
        plaintext = decrypt(ct) if ct else None
        if not plaintext:
            plaintext = await _shared_api_key_for_entry(user_id, entry, model_name)
        base_url = entry.get("base_url")
        # Ollama entries have no base_url; worker falls through to env default.
        return {
            "model": _prefix(model_name),
            "api_base": base_url,
            "api_key": plaintext,
            "extra_params": entry.get("extra_params") or {},
        }

    # 2. Legacy model_pool (Phase E)
    try:
        from services.model_pool import model_pool_service

        resolved = await model_pool_service.get_resolved(user_id, entry_id)
        if resolved:
            return {
                "model": _prefix(resolved["model_name"]),
                "api_base": resolved.get("base_url"),
                "api_key": resolved.get("api_key"),
                "extra_params": resolved.get("extra_params") or {},
            }
    except Exception as exc:  # defensive — legacy service may be stale
        logger.debug("legacy model_pool lookup failed: %s", exc)

    # 3. Legacy model_profiles (Phase 19.3)
    try:
        from services.model_profiles import model_profiles_service

        profile = await model_profiles_service.get_resolved(user_id, entry_id)
        if profile:
            return {
                "model": _prefix(profile["model_name"]),
                "api_base": profile.get("base_url"),
                "api_key": profile.get("api_key"),
                "extra_params": profile.get("extra_params") or {},
            }
    except Exception as exc:
        logger.debug("legacy model_profiles lookup failed: %s", exc)

    return None


async def resolve(user_id: str | None, kind: Kind) -> dict | None:
    """Resolve the user's preferred pool entry for `kind`.

    Precedence chain (Sprint 3):
      (a) settings.models.<kind>.pool_entry_id  — new unified pool
      (b) legacy Phase F user_query_preferences {hyde,agentic,query}_pool_id
          → looked up via the unified-pool resolver which also falls back
          through the legacy collections
    On all misses returns None so the caller substitutes its own default
    (settings.HYDE_MODEL / AGENTIC_MODEL / DEFAULT_COMPLETION_MODEL).
    """
    if not user_id:
        return None

    # Role sections have explicit pool_entry_id fields in ModelsConfig; a
    # "query" default isn't part of the new shape, but Phase F stored one.
    if kind in _KIND_TO_POOL_FIELD:
        from services.settings import settings_service

        raw = await settings_service.get_models_raw(user_id)
        section = raw.get(_KIND_TO_POOL_FIELD[kind]) or {}
        pid = section.get("pool_entry_id")
        if pid:
            result = await resolve_by_entry_id(user_id, pid)
            if result:
                return result
            logger.warning(
                "query_model_resolver: dangling %s pool_entry_id=%s user=%s",
                kind, pid, user_id,
            )

    # Legacy Phase F fallback — honors pre-migration prefs until the next
    # get_settings() call runs the migration.
    from services.query_prefs import query_prefs_service

    legacy_field = {"hyde": "hyde_pool_id", "agentic": "agentic_pool_id",
                    "query": "query_pool_id"}.get(kind)
    if legacy_field:
        prefs = await query_prefs_service.get(user_id)
        legacy_pid = prefs.get(legacy_field)
        if legacy_pid:
            result = await resolve_by_entry_id(user_id, legacy_pid)
            if result:
                return result

    # Sprint 3 follow-up — "query" is the default answer/synthesis lane,
    # but unlike hyde/agentic/reasoning/utility it has no role section with
    # an explicit pool_entry_id. Chat normally sends the selected
    # `pool:<id>` per request; graph synthesis can receive a stale browser
    # selection after the pool is edited. In that case, fall back to the
    # current enabled query pool entry instead of handing LiteLLM a null
    # "(default)" model, which produces a 400 and a deterministic fallback.
    if kind == "query":
        from services.settings import settings_service

        raw = await settings_service.get_models_raw(user_id)
        enabled_entries = [
            entry
            for entry in (raw.get("query_model_pool") or [])
            if isinstance(entry, dict)
            and entry.get("entry_id")
            and entry.get("enabled", True)
        ]
        if enabled_entries:
            result = await resolve_by_entry_id(
                user_id, str(enabled_entries[0]["entry_id"])
            )
            if result:
                return result

    return None

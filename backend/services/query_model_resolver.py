"""
Phase F — Query model resolver.

Reads the user's query prefs doc to map a kind ∈ {hyde, agentic, query} to a
concrete model_pool entry, decrypts the api_key, and returns the 4-tuple
needed to inject per-call overrides into LiteLLM.

When no prefs doc exists, the role is unset, or the referenced pool entry
no longer exists → returns None so the caller can fall back to its
configured server-side default.

This module is QUERY-TIME ONLY. Ingestion-time model resolution lives in
`services/ghost_a.py` and `services/ghost_b.py`, which read from the
per-corpus `IngestionConfig.summary_models` / `extraction_models` snapshot
in MongoDB. The two paths share NO storage or code.
"""

from __future__ import annotations

import logging
from typing import Literal

logger = logging.getLogger(__name__)

Kind = Literal["hyde", "agentic", "query"]
_KIND_TO_FIELD = {
    "hyde": "hyde_pool_id",
    "agentic": "agentic_pool_id",
    "query": "query_pool_id",
}


async def resolve(user_id: str | None, kind: Kind) -> dict | None:
    """Resolve the user's preferred pool entry for `kind`.

    Returns:
        {model, api_base, api_key, extra_params} when resolution succeeds,
        else None. Caller substitutes None with its own fallback (typically
        settings.DEFAULT_COMPLETION_MODEL or settings.HYDE_MODEL).
    """
    if not user_id:
        return None
    field = _KIND_TO_FIELD.get(kind)
    if not field:
        logger.warning("query_model_resolver: unknown kind %r", kind)
        return None

    from services.model_pool import model_pool_service
    from services.query_prefs import query_prefs_service

    prefs = await query_prefs_service.get(user_id)
    pool_id = prefs.get(field)
    if not pool_id:
        return None

    resolved = await model_pool_service.get_resolved(user_id, pool_id)
    if not resolved:
        logger.warning(
            "query_model_resolver: dangling %s=%s for user %s — falling back",
            field, pool_id, user_id,
        )
        return None

    return {
        "model": f"openai/{resolved['model_name']}",
        "api_base": resolved.get("base_url"),
        "api_key": resolved.get("api_key"),
        "extra_params": resolved.get("extra_params") or {},
    }

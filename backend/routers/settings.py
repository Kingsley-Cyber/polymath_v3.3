"""
Settings router — global settings (per user) + infrastructure test endpoints.

Architecture: Settings split into Global (system-wide, mutable) and Per-Corpus
(IngestionConfig, frozen after first ingest). See SETTINGS_ARCHITECTURE.md.

Endpoints:
  GET  /api/settings                                 → GlobalSettingsResponse
  PUT  /api/settings                                 → partial update (mutable settings)
  POST /api/settings/infrastructure/test             → test all 8 services
  POST /api/settings/infrastructure/test/{service}   → test single service
"""

import logging
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from models.schemas import (
    ApiKeysPublic,
    ApiKeysUpdate,
    GlobalSettingsResponse,
    GlobalSettingsUpdate,
    ModelsConfig,
    OllamaBulkAddRequest,
    UtilityModelTestResult,
)
from routers.auth import get_current_user
from services.health_service import health_service
from services.llm import llm_service
from services.query_model_resolver import resolve as resolve_query_model_kind
from services.secrets import KNOWN_PROVIDERS
from services.settings import settings_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["settings"])


# ── Sprint 3 — unified query_model_pool + models subdoc ───────────────────


@router.get("/models", response_model=ModelsConfig)
async def get_models(
    current_user: dict = Depends(get_current_user),
) -> ModelsConfig:
    """Return the caller's `settings.models` section (pool + hyde + agentic).
    api_key_ciphertext values are masked as '[set]' on response — the
    frontend never sees Fernet tokens."""
    all_settings = await settings_service.get_settings(current_user["user_id"])
    return all_settings.models


@router.post("/models", response_model=ModelsConfig)
async def save_models(
    body: ModelsConfig = Body(...),
    current_user: dict = Depends(get_current_user),
) -> ModelsConfig:
    """Replace the caller's full `settings.models` subdoc atomically.
    Handles pool ciphertext preserve/encrypt, validates hyde/agentic
    pool_entry_id references against the post-update pool.
    Returns the persisted view with masked keys."""
    try:
        return await settings_service.update_models(
            current_user["user_id"], body.model_dump()
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_models_config", "reason": str(exc)},
        ) from exc


@router.post("/models/utility/test", response_model=UtilityModelTestResult)
async def test_utility_model_connection(
    current_user: dict = Depends(get_current_user),
) -> UtilityModelTestResult:
    """Run a tiny deterministic call through the configured Utility model."""

    started = time.perf_counter()
    resolved = await resolve_query_model_kind(current_user["user_id"], "utility")

    def latency() -> int:
        return int((time.perf_counter() - started) * 1000)

    if not resolved:
        return UtilityModelTestResult(
            ok=False,
            status="not_configured",
            latency_ms=latency(),
            error="Utility model is not configured.",
        )

    model = str(resolved.get("model") or "").strip()
    if not model:
        return UtilityModelTestResult(
            ok=False,
            status="invalid_config",
            latency_ms=latency(),
            error="Utility model entry is missing a model name.",
        )

    try:
        output = await llm_service.complete_sync(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a Polymath model-pool connection tester. "
                        "Follow the user's instruction exactly."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Return exactly this text and nothing else: "
                        "POLYMATH_UTILITY_OK"
                    ),
                },
            ],
            model=model,
            temperature=0.0,
            max_tokens=24,
            api_base=resolved.get("api_base"),
            api_key=resolved.get("api_key"),
            extra_params=resolved.get("extra_params") or {},
            timeout=45.0,
        )
    except Exception as exc:
        return UtilityModelTestResult(
            ok=False,
            status="error",
            model=model,
            latency_ms=latency(),
            error=f"{type(exc).__name__}: {str(exc)[:300]}",
        )

    preview = " ".join(str(output or "").split())[:160] or None
    if not preview:
        return UtilityModelTestResult(
            ok=False,
            status="empty_response",
            model=model,
            latency_ms=latency(),
            error="Utility model returned an empty response.",
        )

    return UtilityModelTestResult(
        ok=True,
        status="ok",
        model=model,
        latency_ms=latency(),
        output_preview=preview,
    )


@router.post("/models/ollama/add", response_model=ModelsConfig)
async def bulk_add_ollama(
    body: OllamaBulkAddRequest = Body(...),
    current_user: dict = Depends(get_current_user),
) -> ModelsConfig:
    """Bulk-create ollama pool entries. Idempotent by (provider=ollama,
    model_name) — duplicates silently skipped."""
    return await settings_service.add_ollama_entries(
        current_user["user_id"], body.model_names
    )


@router.delete("/models/pool/{entry_id}", response_model=ModelsConfig)
async def delete_pool_entry(
    entry_id: str,
    current_user: dict = Depends(get_current_user),
) -> ModelsConfig:
    """Remove one pool entry by id. If hyde/agentic pool_entry_id pointed
    at the removed entry it is nulled silently (resolver falls through to
    the legacy fallback chain)."""
    return await settings_service.delete_pool_entry(
        current_user["user_id"], entry_id
    )


@router.get("", response_model=GlobalSettingsResponse)
async def get_settings(
    current_user: dict = Depends(get_current_user),
) -> GlobalSettingsResponse:
    """
    Get global settings for the current user.

    Returns infrastructure (read-only from .env), chat defaults, and retrieval defaults.
    Sensitive fields (API keys, passwords) are masked as ••••••••.
    """
    settings = await settings_service.get_settings(current_user["user_id"])
    return GlobalSettingsResponse(settings=settings)


@router.put("", response_model=GlobalSettingsResponse)
async def update_settings(
    patch: GlobalSettingsUpdate = Body(...),
    current_user: dict = Depends(get_current_user),
) -> GlobalSettingsResponse:
    """
    Partial update of global settings.

    Chat, retrieval, modal, models, extraction, and ingestion defaults are mutable.
    'infrastructure' is always read from config.py (env vars) — cannot be changed via API.
    Omitted sections are left unchanged.
    """
    # Convert to dict, excluding None values (partial update)
    patch_dict = patch.model_dump(exclude_none=True)
    if not patch_dict:
        raise HTTPException(status_code=400, detail="No fields to update")

    settings = await settings_service.update_settings(current_user["user_id"], patch_dict)
    return GlobalSettingsResponse(settings=settings)


@router.get("/extraction/validate")
async def validate_extraction(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """
    Probe every configured extraction endpoint (enabled or not) from the
    backend's own network position and return per-endpoint deploy-readiness
    checklists plus an overall deploy_ready verdict. Read-only.
    """
    from services.extraction_validation import validate_endpoints

    settings = await settings_service.get_settings(current_user["user_id"])
    extraction = getattr(settings, "extraction", None)
    raw = getattr(extraction, "endpoints", None) or []
    endpoints = [e.model_dump() if hasattr(e, "model_dump") else dict(e) for e in raw]
    return await validate_endpoints(endpoints)


@router.post("/infrastructure/test")
async def test_all_services(
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Test connectivity to all 8 infrastructure services.

    Returns a dict mapping service name → {status, latency_ms, error?}.
    Services: mongodb, qdrant, litellm, ollama, neo4j (if enabled),
              redis, embedder, reranker, modal (if enabled).
    """
    result = await settings_service.test_infrastructure()
    return {"services": result}


@router.post("/infrastructure/test/{service_name}")
async def test_single_service(
    service_name: str,
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Test connectivity to a single infrastructure service.

    Valid service names: mongodb, qdrant, neo4j, litellm, ollama, redis,
                        embedder, reranker, modal.
    """
    check_methods = {
        "mongodb": health_service.check_mongodb,
        "qdrant": health_service.check_qdrant,
        "neo4j": health_service.check_neo4j,
        "litellm": health_service.check_litellm,
        "ollama": health_service.check_ollama,
        "redis": health_service.check_redis,
        "embedder": health_service.check_embedder,
        "reranker": health_service.check_reranker,
        "modal": health_service.check_modal,
        "siliconflow": health_service.check_siliconflow,
    }

    if service_name not in check_methods:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown service: {service_name}. Valid: {', '.join(check_methods.keys())}",
        )

    result = await check_methods[service_name]()
    return {
        "service": service_name,
        "status": result.status,
        "latency_ms": result.latency_ms,
        "error": result.error,
    }


# ── Modal BYOK — verify saved tokens via `modal token info` ────────────────


@router.post("/infrastructure/modal/verify-token")
async def verify_modal_token(
    body: dict[str, Any] | None = Body(default=None),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Verify a pair of Modal tokens by shelling out to `modal token info`.

    Request body (all optional):
        {
          "token_id":     str | None,   # override saved creds for this call
          "token_secret": str | None,
        }

    If token_id/token_secret are omitted, the user's saved
    api_keys.modal_token_id + api_keys.modal_token_secret are used.

    Returns:
        {
          "ok": bool,
          "workspace": str | None,
          "error": str | None,
        }

    No deploy, no GPU, no cost. `modal token info` pings Modal's API and
    echoes the workspace name if creds are valid. Timeout 10s.
    """
    import asyncio

    body = body or {}
    token_id = (body.get("token_id") or "").strip() or None
    token_secret = (body.get("token_secret") or "").strip() or None

    if not token_id or not token_secret:
        saved = await settings_service.get_plaintext_keys_for_llm(
            current_user["user_id"]
        )
        token_id = token_id or saved.get("modal_token_id")
        token_secret = token_secret or saved.get("modal_token_secret")

    if not token_id or not token_secret:
        return {
            "ok": False,
            "workspace": None,
            "error": (
                "Modal tokens not configured. Save modal_token_id + "
                "modal_token_secret under Settings → API Keys first."
            ),
        }

    # Modal SDK auth check. Client.verify() makes one gRPC call to api.modal.com
    # and raises AuthError on bad creds. ~1s, no container, no cost.
    # The CLI's `modal token info` does not exist in modal==0.64.x; `modal app
    # list` etc. swallow auth failures to exit 0, so subprocess is unreliable.
    try:
        import modal.client as mc
        import modal.config as modal_cfg

        server_url = (
            modal_cfg.config.get("server_url") or "https://api.modal.com"
        )
        try:
            await asyncio.wait_for(
                mc.Client.verify(server_url, (token_id, token_secret)),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "workspace": None,
                "error": "Modal gRPC verify timed out after 10s.",
            }
    except Exception as exc:
        # AuthError, ConnectionError, VersionError — all land here.
        return {
            "ok": False,
            "workspace": None,
            "error": f"{type(exc).__name__}: {exc}",
        }

    # Verify succeeded. We don't have a cheap way to pull the workspace slug
    # back from the server with SDK 0.64, so leave workspace as the stored
    # value. The UI keeps it as an editable field; the user fills it in once.
    return {"ok": True, "workspace": None, "error": None}


# ── Phase 19.2 — API key manager (Fernet-encrypted at rest) ────────────────


@router.get("/api-keys", response_model=ApiKeysPublic)
async def get_api_keys(
    current_user: dict = Depends(get_current_user),
) -> ApiKeysPublic:
    """
    Return MASKED api keys for the current user. Plaintext NEVER leaves the
    backend — the response shows `sk-****abc4` style or `[not set]`.
    """
    masked = await settings_service.get_api_keys_masked(current_user["user_id"])
    return ApiKeysPublic(keys=masked, providers=sorted(KNOWN_PROVIDERS))


@router.put("/api-keys", response_model=ApiKeysPublic)
async def update_api_keys(
    body: ApiKeysUpdate = Body(...),
    current_user: dict = Depends(get_current_user),
) -> ApiKeysPublic:
    """
    Save plaintext api keys. Each provider's value is Fernet-encrypted before
    storage. Empty value clears that provider's key. Returns the fresh masked
    view so the UI can immediately reflect the new state.
    """
    if not body.keys:
        raise HTTPException(status_code=400, detail="No keys supplied")

    try:
        masked = await settings_service.update_api_keys(
            current_user["user_id"], body.keys
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return ApiKeysPublic(keys=masked, providers=sorted(KNOWN_PROVIDERS))

"""
Phase E — Unified Model Pool router.

CRUD + test for per-user model pool entries. Plaintext keys encrypted at
rest; only masked views round-trip to the frontend.
"""
import logging

from fastapi import APIRouter, Body, Depends, HTTPException, status
from models.schemas import (
    ModelPoolEntryCreate,
    ModelPoolEntryPublic,
    ModelPoolEntryUpdate,
    ModelPoolListResponse,
    ModelPoolTestResult,
)
from routers.auth import get_current_user
from services.model_pool import model_pool_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/model-pool", tags=["model-pool"])


@router.get("", response_model=ModelPoolListResponse)
async def list_pool(
    current_user: dict = Depends(get_current_user),
) -> ModelPoolListResponse:
    # Best-effort one-shot migration from Phase B profiles the first time the
    # pool is hit for this user.
    try:
        await model_pool_service.migrate_from_legacy(current_user["user_id"])
    except Exception as exc:
        logger.warning("Pool migration skipped: %s", exc)
    entries = await model_pool_service.list_for_user(current_user["user_id"])
    return ModelPoolListResponse(
        entries=[ModelPoolEntryPublic(**e) for e in entries]
    )


@router.post(
    "", response_model=ModelPoolEntryPublic, status_code=status.HTTP_201_CREATED
)
async def create_entry(
    body: ModelPoolEntryCreate = Body(...),
    current_user: dict = Depends(get_current_user),
) -> ModelPoolEntryPublic:
    entry = await model_pool_service.create(
        user_id=current_user["user_id"],
        label=body.label,
        provider=body.provider,
        base_url=body.base_url,
        model_name=body.model_name,
        api_key=body.api_key,
        use_shared_key=body.use_shared_key,
        extra_params=body.extra_params or {},
        context_length=body.context_length,
        tags=body.tags or ["chat"],
        enabled=body.enabled,
    )
    return ModelPoolEntryPublic(**entry)


@router.put("/{entry_id}", response_model=ModelPoolEntryPublic)
async def update_entry(
    entry_id: str,
    body: ModelPoolEntryUpdate = Body(...),
    current_user: dict = Depends(get_current_user),
) -> ModelPoolEntryPublic:
    patch = body.model_dump(exclude_none=True)
    entry = await model_pool_service.update(
        current_user["user_id"], entry_id, patch
    )
    if not entry:
        raise HTTPException(status_code=404, detail="Pool entry not found")
    return ModelPoolEntryPublic(**entry)


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entry(
    entry_id: str,
    current_user: dict = Depends(get_current_user),
) -> None:
    ok = await model_pool_service.delete(current_user["user_id"], entry_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Pool entry not found")


@router.post("/{entry_id}/test", response_model=ModelPoolTestResult)
async def test_entry(
    entry_id: str,
    current_user: dict = Depends(get_current_user),
) -> ModelPoolTestResult:
    result = await model_pool_service.test_connection(
        current_user["user_id"], entry_id
    )
    return ModelPoolTestResult(**result)


@router.post("/test-inline", response_model=ModelPoolTestResult)
async def test_inline(
    payload: dict = Body(...),
    current_user: dict = Depends(get_current_user),
) -> ModelPoolTestResult:
    """Connectivity probe for a lane that hasn't been saved to the pool yet.

    Used by the corpus IngestionLanesSection chips so operators can verify
    a cloud lane works BEFORE saving it on the corpus. Same one-token-ping
    semantics as the saved-entry /test endpoint, but takes the lane shape
    inline:
      {
        "model":    "deepseek-chat" | "mistral-small-latest" | …,
        "base_url": "https://api.deepseek.com/v1" | … | null,
        "api_key":  plaintext (or omitted for local lanes that don't need one),
        "kind":     "chat" | "rerank" | "embed"   (default "chat")
      }
    """
    import time
    import httpx
    model = str(payload.get("model") or "").strip()
    if not model:
        return ModelPoolTestResult(ok=False, error="model is required")
    base_url = (payload.get("base_url") or "").rstrip("/")
    api_key = payload.get("api_key") or ""
    kind = str(payload.get("kind") or "chat").lower()

    if kind == "rerank":
        # Reranker exposes /health (no auth, container-internal). The lane
        # config doesn't normally pass through LiteLLM — it's a direct call
        # from chat_orchestrator. Test by probing /health.
        url = f"{base_url}/health" if base_url else "http://reranker:8080/health"
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
            latency_ms = int((time.monotonic() - started) * 1000)
            if resp.status_code >= 400:
                return ModelPoolTestResult(
                    ok=False, status=resp.status_code, latency_ms=latency_ms,
                    error=resp.text[:250],
                )
            return ModelPoolTestResult(ok=True, status=resp.status_code, latency_ms=latency_ms)
        except Exception as exc:
            return ModelPoolTestResult(ok=False, error=str(exc)[:250])

    if kind == "embed":
        url = f"{base_url}/embeddings" if base_url else None
        if not url:
            return ModelPoolTestResult(ok=False, error="base_url required for embed kind")
        body = {"model": model, "input": "ping"}
    else:  # chat (default)
        # Strip any LiteLLM provider prefix the UI may have prepended
        # (e.g. "deepseek/deepseek-chat" → "deepseek-chat") since cloud
        # provider native APIs reject prefixed model names.
        bare_model = model.split("/", 1)[1] if "/" in model else model
        url = f"{base_url}/chat/completions" if base_url else None
        if not url:
            return ModelPoolTestResult(ok=False, error="base_url required for chat kind")
        body = {
            "model": bare_model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
            "stream": False,
        }

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=body, headers=headers)
        latency_ms = int((time.monotonic() - started) * 1000)
        if resp.status_code >= 400:
            return ModelPoolTestResult(
                ok=False, status=resp.status_code, latency_ms=latency_ms,
                error=resp.text[:250],
            )
        return ModelPoolTestResult(ok=True, status=resp.status_code, latency_ms=latency_ms)
    except httpx.TimeoutException:
        return ModelPoolTestResult(ok=False, error="Request timed out after 15s")
    except Exception as exc:
        return ModelPoolTestResult(ok=False, error=str(exc)[:250])

# backend/routers/models.py
# GET /api/models — merged model list from Ollama + LiteLLM + local ./download folder
# Thin router: validate → call service → return. No business logic here.

import asyncio
import json
import logging
from pathlib import Path

import httpx
from config import get_settings
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from models.schemas import ModelInfo, ModelsResponse
from routers.auth import get_current_user
from services.auth import auth_service

router = APIRouter(prefix="/api", tags=["models"])
logger = logging.getLogger(__name__)
settings = get_settings()


async def _current_user_optional(request: Request) -> dict | None:
    """Best-effort JWT extraction — returns None instead of 401 when the
    Authorization header is missing or invalid. Used by /api/models so the
    endpoint stays public for unauthenticated probes but applies per-user
    ollama exclusions when a valid bearer token is present.
    """
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    token = auth.split(None, 1)[1].strip()
    try:
        payload = auth_service.verify_token(token)
    except Exception:
        return None
    if payload is None:
        return None
    return {"user_id": payload.user_id, "username": payload.username}

# Name fragments that signal embedding intent
_EMBEDDING_NAME_HINTS = {"embed", "embedding", "e5", "bge", "gte", "minilm", "mpnet"}


def _is_embedding_name(name: str) -> bool:
    lower = name.lower()
    return any(hint in lower for hint in _EMBEDDING_NAME_HINTS)


def _make_display_name(raw: str) -> str:
    return (
        raw.replace("/", " / ")
        .replace("-", " ")
        .replace(":", " ")
        .replace("_", " ")
        .title()
        .strip()
    )


# ─────────────────────────────────────────────
# SOURCE 1: Ollama
# ─────────────────────────────────────────────

async def get_ollama_models() -> list[ModelInfo]:
    """Fetch models from Ollama /api/tags."""
    models: list[ModelInfo] = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.OLLAMA_URL}/api/tags")
            if resp.status_code != 200:
                logger.warning(f"Ollama returned {resp.status_code}")
                return models

            for m in resp.json().get("models", []):
                name: str = m.get("name", "")
                if not name:
                    continue

                details = m.get("details", {})
                is_embed = _is_embedding_name(name)

                models.append(ModelInfo(
                    id=f"ollama/{name}",
                    name=_make_display_name(name),
                    provider="ollama",
                    source="ollama",
                    type="embedding" if is_embed else "chat",
                    context_length=details.get("context_length"),
                    dimension=None,
                ))

        logger.debug(f"Ollama: {len(models)} models")
    except httpx.TimeoutException:
        logger.warning("Ollama request timed out")
    except httpx.ConnectError:
        logger.warning("Cannot connect to Ollama")
    except Exception as e:
        logger.error(f"Ollama fetch error: {e}")

    return models


# ─────────────────────────────────────────────
# SOURCE 2: LiteLLM (cloud + configured providers)
# ─────────────────────────────────────────────

async def get_litellm_models() -> list[ModelInfo]:
    """Fetch configured models from LiteLLM proxy /models."""
    models: list[ModelInfo] = []
    try:
        headers = {}
        if settings.LITELLM_MASTER_KEY:
            headers["Authorization"] = f"Bearer {settings.LITELLM_MASTER_KEY}"

        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.LITELLM_URL}/models", headers=headers)
            if resp.status_code != 200:
                logger.warning(f"LiteLLM returned {resp.status_code}")
                return models

            for m in resp.json().get("data", []):
                model_id: str = m.get("id", "")
                if not model_id:
                    continue

                # Skip tei/* — local models surfaced via download scanner instead
                if model_id.startswith("tei/"):
                    continue

                provider = model_id.split("/")[0] if "/" in model_id else "unknown"

                models.append(ModelInfo(
                    id=model_id,
                    name=_make_display_name(model_id),
                    provider=provider,
                    source="litellm",
                    type="embedding" if _is_embedding_name(model_id) else "chat",
                    context_length=None,
                    dimension=None,
                ))

        logger.debug(f"LiteLLM: {len(models)} models")
    except httpx.TimeoutException:
        logger.warning("LiteLLM request timed out")
    except httpx.ConnectError:
        logger.warning("Cannot connect to LiteLLM")
    except Exception as e:
        logger.error(f"LiteLLM fetch error: {e}")

    return models


# ─────────────────────────────────────────────
# SOURCE 3: Live embedder service (/info)
# ─────────────────────────────────────────────

async def get_embedder_model() -> list[ModelInfo]:
    """
    Query the running embedder service /info endpoint.
    Returns the live model with introspected dimension.
    This is authoritative — the embedder reports exactly what it loaded.
    """
    models: list[ModelInfo] = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.EMBEDDER_URL}/info")
            if resp.status_code != 200:
                logger.warning(f"Embedder /info returned {resp.status_code}")
                return models

            info = resp.json()
            name = info.get("model_name", settings.EMBEDDER_MODEL_NAME)
            dim = info.get("dimension")
            device = info.get("device", "unknown")

            models.append(ModelInfo(
                id=f"tei/{name}",
                name=_make_display_name(name),
                provider="local",
                source="embedder",
                type="embedding",
                context_length=None,
                dimension=dim,
            ))

            logger.debug(f"Embedder live: tei/{name} dim={dim} device={device}")

    except httpx.TimeoutException:
        logger.warning("Embedder /info timed out — service may still be loading")
    except httpx.ConnectError:
        logger.warning("Cannot connect to embedder service")
    except Exception as e:
        logger.error(f"Embedder /info error: {e}")

    return models


# ─────────────────────────────────────────────
# SOURCE 4: Local download folder
# ─────────────────────────────────────────────

def get_local_models() -> list[ModelInfo]:
    """
    Scan MODELS_DIR for HuggingFace model directories.

    Valid directory = contains config.json.
    Reads hidden_size for embedding dimension.
    Uses config_sentence_transformers.json presence as embedding marker.
    """
    models: list[ModelInfo] = []
    models_dir = Path(settings.MODELS_DIR)

    if not models_dir.exists():
        logger.warning(f"MODELS_DIR {models_dir} does not exist — no local models loaded")
        return models

    for entry in sorted(models_dir.iterdir()):
        if not entry.is_dir():
            continue

        config_path = entry / "config.json"
        if not config_path.exists():
            continue

        try:
            with open(config_path) as f:
                cfg = json.load(f)

            # Sentence-transformers marker = definitive embedding signal
            st_config = entry / "config_sentence_transformers.json"
            is_embed = st_config.exists() or _is_embedding_name(entry.name)

            # Architecture fallback: generation archs are NOT embedders
            if not is_embed:
                archs = cfg.get("architectures", [])
                if archs:
                    is_embed = not any(
                        k in archs[0] for k in ("ForCausalLM", "ForSeq2Seq", "ForConditional")
                    )

            model_type = "embedding" if is_embed else "chat"

            # hidden_size is the standard HF embedding dimension field
            dimension: int | None = (
                cfg.get("hidden_size")
                or cfg.get("d_model")
                or cfg.get("dim")
            )

            models.append(ModelInfo(
                id=f"local/{entry.name}",
                name=_make_display_name(entry.name),
                provider="local",
                source="download",
                type=model_type,
                context_length=cfg.get("max_position_embeddings") if model_type == "chat" else None,
                dimension=dimension if model_type == "embedding" else None,
            ))

            logger.debug(f"Local model: local/{entry.name} type={model_type} dim={dimension}")

        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read {config_path}: {e}")

    logger.debug(f"Local: {len(models)} models from {models_dir}")
    return models


# ─────────────────────────────────────────────
# MERGE + SPLIT
# ─────────────────────────────────────────────

def _merge_and_split(
    ollama: list[ModelInfo],
    litellm: list[ModelInfo],
    embedder: list[ModelInfo],
    local: list[ModelInfo],
) -> tuple[list[ModelInfo], list[ModelInfo]]:
    """
    Deduplicate by id, split into chat vs embedding lists.
    Priority: embedder (live) > local (filesystem) > ollama > litellm
    Embedder wins because it reports the actual loaded model + real dimension.
    """
    merged: dict[str, ModelInfo] = {}

    for m in litellm:
        merged[m.id] = m
    for m in ollama:
        merged[m.id] = m
    for m in local:
        merged[m.id] = m
    for m in embedder:
        merged[m.id] = m  # highest priority — live introspected

    all_models = sorted(merged.values(), key=lambda m: (m.provider, m.name))

    return (
        [m for m in all_models if m.type == "chat"],
        [m for m in all_models if m.type == "embedding"],
    )


# ─────────────────────────────────────────────
# ENDPOINT
# ─────────────────────────────────────────────

@router.get("/models", response_model=ModelsResponse)
async def list_models(
    current_user: dict | None = Depends(_current_user_optional),
    reachable: bool = True,
):
    """
    GET /api/models

    Merges models from three sources:
    - Ollama (locally pulled models via ollama pull)
    - LiteLLM (configured cloud providers: openai, anthropic, deepseek, gemini)
    - ./download folder (local HF safetensors models, e.g. Qwen3-Embedding-0.6B)

    Response splits into chat_models and embedding_models
    for use in separate frontend dropdowns.

    Phase F — when a valid bearer token is present, the user's
    ollama_exclusions list (from user_query_preferences) filters out
    matching ollama models from chat_models before returning. Anonymous
    callers see the unfiltered list.
    """
    ollama_result, litellm_result, embedder_result = await asyncio.gather(
        get_ollama_models(),
        get_litellm_models(),
        get_embedder_model(),
        return_exceptions=True,
    )

    if isinstance(ollama_result, Exception):
        logger.error(f"Ollama gather error: {ollama_result}")
        ollama_result = []

    if isinstance(litellm_result, Exception):
        logger.error(f"LiteLLM gather error: {litellm_result}")
        litellm_result = []

    if isinstance(embedder_result, Exception):
        logger.error(f"Embedder gather error: {embedder_result}")
        embedder_result = []

    # Filesystem scan — run in thread pool to avoid blocking event loop
    local_result = await asyncio.get_event_loop().run_in_executor(
        None, get_local_models
    )

    chat_models, embedding_models = _merge_and_split(
        ollama_result, litellm_result, embedder_result, local_result
    )

    # Phase F — apply per-user ollama exclusions when authenticated.
    if current_user:
        try:
            from services.query_prefs import query_prefs_service

            prefs = await query_prefs_service.get(current_user["user_id"])
            excluded = set(prefs.get("ollama_exclusions") or [])
            if excluded:
                chat_models = [m for m in chat_models if m.id not in excluded]
        except Exception as exc:
            logger.warning("ollama exclusions skipped (%s)", exc)

    # Sprint 3 — ?reachable=true (default): filter the LiteLLM catalog to
    # providers the caller has at least one pool entry for. Ollama +
    # embedder paths are always reachable; we only narrow the cloud slice.
    if reachable and current_user:
        try:
            from services.settings import settings_service

            raw = await settings_service.get_models_raw(current_user["user_id"])
            pool_providers = {
                (e.get("provider") or "").lower()
                for e in (raw.get("query_model_pool") or [])
                if isinstance(e, dict) and e.get("enabled", True)
            }
            if pool_providers:
                def _keep(m: ModelInfo) -> bool:
                    if m.source != "litellm":
                        return True  # ollama + local always kept
                    # LiteLLM model ids are "provider/model" → check the prefix
                    provider = (m.id.split("/", 1)[0] or "").lower() if "/" in m.id else ""
                    return provider in pool_providers or m.provider.lower() in pool_providers
                chat_models = [m for m in chat_models if _keep(m)]
        except Exception as exc:
            logger.warning("reachable filter skipped (%s)", exc)

    return ModelsResponse(
        chat_models=chat_models,
        embedding_models=embedding_models,
        default_model=settings.DEFAULT_COMPLETION_MODEL,
        default_embedding_model=settings.DEFAULT_EMBEDDING_MODEL,
    )


# ============================================================================
# Phase 19 Wave 1 — Ollama Model Manager
# ============================================================================
#
# Thin proxy over Ollama's native management API:
#   GET    /api/models/ollama/installed  → list installed models (size, modified)
#   POST   /api/models/ollama/pull       → pull a model, stream progress as SSE
#   DELETE /api/models/ollama            → delete model by name
#
# All three are auth-gated. Pull uses SSE because Ollama's /api/pull streams
# JSONL status updates over minutes for large models — same shape we forward.


@router.get("/models/ollama/installed")
async def list_ollama_installed(current_user: dict = Depends(get_current_user)):
    """
    List every model currently installed on the Ollama server.

    Shape per entry: {name, size_bytes, size_human, modified_at, digest, details}
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{settings.OLLAMA_URL}/api/tags")
            resp.raise_for_status()
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot reach Ollama at {settings.OLLAMA_URL}: {exc}",
        )

    raw = resp.json().get("models", [])
    out = []
    for m in raw:
        size_bytes = int(m.get("size") or 0)
        out.append(
            {
                "name": m.get("name", ""),
                "size_bytes": size_bytes,
                "size_human": _human_size(size_bytes),
                "modified_at": m.get("modified_at"),
                "digest": (m.get("digest") or "")[:12],
                "details": m.get("details", {}),
            }
        )
    return {"models": out, "count": len(out)}


def _human_size(n: int) -> str:
    """Format bytes as human-readable (KiB/MiB/GiB)."""
    if n <= 0:
        return "0 B"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n = n / 1024
    return f"{n:.1f} PiB"


@router.post("/models/ollama/pull")
async def pull_ollama_model(
    body: dict = Body(...),
    current_user: dict = Depends(get_current_user),
):
    """
    Pull a model by name. Forwards Ollama's streaming progress as SSE.

    Request body: {"name": "llama3.2:3b"}
    Response: text/event-stream with events of shape
      data: {"status": "pulling manifest"}
      data: {"status": "downloading", "completed": 123, "total": 456, "digest": "..."}
      data: {"status": "success"}
      data: {"error": "..."}
    """
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Missing 'name' in request body")

    async def event_stream():
        """Stream Ollama's JSONL progress as SSE."""
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{settings.OLLAMA_URL}/api/pull",
                    json={"name": name, "stream": True},
                    timeout=None,
                ) as resp:
                    if resp.status_code != 200:
                        txt = (await resp.aread()).decode(errors="replace")
                        err_msg = f"Ollama returned {resp.status_code}: {txt}"
                        yield f'data: {json.dumps({"error": err_msg})}\n\n'
                        return
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        # Ollama emits one JSON object per line
                        yield f"data: {line}\n\n"
        except httpx.RequestError as exc:
            yield f'data: {json.dumps({"error": f"Connection error: {exc}"})}\n\n'
        except Exception as exc:  # pragma: no cover — defensive
            logger.exception("Unexpected error in ollama pull stream")
            yield f'data: {json.dumps({"error": str(exc)})}\n\n'

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering for SSE
        },
    )


@router.delete("/models/ollama")
async def delete_ollama_model(
    body: dict = Body(...),
    current_user: dict = Depends(get_current_user),
):
    """
    Delete a model from the Ollama server.

    Request body: {"name": "llama3.2:3b"}
    """
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Missing 'name' in request body")

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                "DELETE",
                f"{settings.OLLAMA_URL}/api/delete",
                json={"name": name},
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Cannot reach Ollama at {settings.OLLAMA_URL}: {exc}",
        )

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail=f"Model '{name}' not found")
    if resp.status_code != 200:
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"Ollama delete failed: {resp.text}",
        )
    return {"ok": True, "deleted": name}

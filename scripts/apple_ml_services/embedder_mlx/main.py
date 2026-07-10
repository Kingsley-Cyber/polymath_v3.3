"""Apple Silicon MLX embedder sidecar - FastAPI, host-native.

Mounts an OpenAI-compatible /embeddings endpoint plus /info and /health,
matching the contract of the in-cluster embedder service so the rest of
Polymath doesn't notice the swap.

Wire spec (matches backend expectations):
  GET  /info       → {"model": "...", "dimension": 1024, "device": "mps"}
  GET  /health     → {"status": "ok"}
  POST /embeddings → OpenAI shape {data: [{embedding: [...], index: i}, ...]}

Required env:
  APPLE_MLX_EMBED_MODEL_ID default mlx-community/Qwen3-Embedding-0.6B-mxfp8
  EMBEDDER_PORT          default 8082 (set by start.sh)
  EMBED_MAX_LENGTH       default 512
  EMBED_BATCH_SIZE       default 32
  HF_HOME                provided by LaunchAgent
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("embedder_mlx")
logging.basicConfig(level=logging.INFO)

MODEL_ID = os.environ.get(
    "APPLE_MLX_EMBED_MODEL_ID",
    os.environ.get("EMBED_MODEL_ID", "mlx-community/Qwen3-Embedding-0.6B-mxfp8"),
)
MODEL_NAME = os.environ.get("EMBEDDER_MODEL_NAME", "Qwen3-Embedding-0.6B")
EMBED_DIM = 1024
MAX_LENGTH = int(os.environ.get("EMBED_MAX_LENGTH", "512"))
BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", "32"))
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("EMBEDDER_REQUEST_TIMEOUT_SECONDS", "60"))
QUEUE_TIMEOUT_SECONDS = float(os.environ.get("EMBEDDER_QUEUE_TIMEOUT_SECONDS", "30"))


class EmbeddingsRequest(BaseModel):
    input: list[str] | str
    model: str | None = None  # ignored — we always serve MODEL_ID


class EmbeddingItem(BaseModel):
    object: str = "embedding"
    embedding: list[float]
    index: int


class EmbeddingsResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingItem]
    model: str = MODEL_ID
    usage: dict[str, int] = Field(default_factory=lambda: {"prompt_tokens": 0, "total_tokens": 0})


app = FastAPI(title="Polymath Apple MLX Embedder", version="0.1.0")
_request_gate = asyncio.Semaphore(1)
_active_request_started_at: float | None = None
_last_request_seconds: float | None = None
_last_error: str | None = None


def _apply_mlx_memory_guardrails() -> None:
    """Owner infra rule (POLYMATH_ARCHITECTURE §4.5): MLX services cap their
    Metal buffer cache so embedder + reranker + (future) answer LLM never
    fight for unified memory. Tunable via MLX_CACHE_LIMIT_GB (default 1.0);
    startup metrics logged so memory_pressure investigations have a baseline.
    """
    import os

    try:
        import mlx.core as mx

        gb = 1024 ** 3
        limit = float(os.environ.get("MLX_CACHE_LIMIT_GB", "1.0") or 1.0)
        mx.set_cache_limit(int(limit * gb))
        print(
            f"[mlx-guardrail] cache_limit={limit:.1f}GB "
            f"metal={getattr(mx, 'metal', None) and mx.metal.is_available()} "
            f"active={mx.get_active_memory() / gb:.2f}GB "
            f"peak={mx.get_peak_memory() / gb:.2f}GB "
            f"cache={mx.get_cache_memory() / gb:.2f}GB",
            flush=True,
        )
    except Exception as exc:  # never block startup on metrics
        print(f"[mlx-guardrail] unavailable: {exc}", flush=True)


_apply_mlx_memory_guardrails()
_model: Any = None
_tokenizer: Any = None
_generate: Any = None


def _import_mlx_embeddings() -> tuple[Any, Any]:
    try:
        from mlx_embeddings import generate, load

        return load, generate
    except ImportError:
        try:
            from mlx_embeddings.utils import generate, load

            return load, generate
        except ImportError as exc:
            raise RuntimeError(
                "mlx-embeddings not installed. Run scripts/install_apple_mlx_runtime.sh"
            ) from exc


def _load_model() -> None:
    """Load Qwen3 embedding model via mlx-embeddings.

    The mlx-embeddings library is the lightweight wrapper around mlx that
    exposes pooled sentence embeddings for Qwen3 / BGE / E5 families.
    """
    global _generate, _model, _tokenizer
    if _model is not None:
        return
    load, generate = _import_mlx_embeddings()

    logger.info("loading %s", MODEL_ID)
    _model, _tokenizer = load(MODEL_ID)
    _generate = generate
    logger.info("model ready (dim=%d, max_len=%d)", EMBED_DIM, MAX_LENGTH)


def _as_numpy(value: Any) -> Any:
    import numpy as np

    try:
        import mlx.core as mx

        mx.eval(value)
    except Exception:
        pass
    return np.asarray(value)


def _extract_embeddings(output: Any) -> Any:
    if hasattr(output, "text_embeds"):
        return _as_numpy(output.text_embeds)
    if hasattr(output, "sentence_embedding"):
        return _as_numpy(output.sentence_embedding)
    if hasattr(output, "pooler_output"):
        return _as_numpy(output.pooler_output)
    if isinstance(output, dict):
        for key in ("text_embeds", "sentence_embedding", "pooler_output"):
            if key in output:
                return _as_numpy(output[key])
    raise RuntimeError(
        "MLX embedder returned an unrecognised output; expected text_embeds, "
        "sentence_embedding, or pooler_output."
    )


def _normalise_rows(vectors: Any) -> Any:
    import numpy as np

    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms = np.where(norms > 0, norms, 1.0)
    return arr / norms


def _encode_batch(inputs: list[str]) -> Any:
    if _model is None or _tokenizer is None:
        _load_model()

    if _generate is not None:
        try:
            output = _generate(_model, _tokenizer, texts=inputs)
            return _normalise_rows(_extract_embeddings(output))
        except TypeError:
            output = _generate(_model, _tokenizer, inputs)
            return _normalise_rows(_extract_embeddings(output))
        except Exception as exc:
            logger.warning("mlx-embeddings.generate failed; falling back to direct call: %s", exc)

    try:
        toks = _tokenizer(
            inputs,
            padding=True,
            truncation=True,
            max_length=MAX_LENGTH,
            return_tensors="np",
        )
        try:
            result = _model(toks["input_ids"], attention_mask=toks.get("attention_mask"))
        except TypeError:
            result = _model(toks["input_ids"])
        return _normalise_rows(_extract_embeddings(result))
    except Exception as exc:
        raise RuntimeError(f"embedding failed: {exc}") from exc


def _encode_texts(inputs: list[str]) -> Any:
    import numpy as np

    batches = []
    for start in range(0, len(inputs), max(1, BATCH_SIZE)):
        batches.append(_encode_batch(inputs[start : start + BATCH_SIZE]))
    embeddings_np = np.vstack(batches)
    if embeddings_np.shape[1] != EMBED_DIM:
        raise RuntimeError(
            f"embedding dimension mismatch: expected {EMBED_DIM}, got {embeddings_np.shape[1]}"
        )
    return embeddings_np


@app.on_event("startup")
async def _startup() -> None:
    try:
        _load_model()
    except Exception as exc:  # pragma: no cover — surface to logs, allow /health to still answer
        logger.exception("startup model load failed: %s", exc)


@app.get("/info")
async def info() -> dict:
    return {
        "model": MODEL_ID,
        "model_name": MODEL_NAME,
        "dimension": EMBED_DIM,
        "device": "mps",
        "max_length": MAX_LENGTH,
        "batch_size": BATCH_SIZE,
        "ready": _model is not None,
    }


@app.get("/health")
async def health() -> dict:
    if _model is None:
        raise HTTPException(status_code=503, detail="model is not loaded")
    now = time.monotonic()
    stalled_for = (
        now - _active_request_started_at
        if _active_request_started_at is not None
        else 0.0
    )
    if stalled_for > REQUEST_TIMEOUT_SECONDS:
        raise HTTPException(
            status_code=503,
            detail=(
                f"embedding request stalled for {stalled_for:.1f}s "
                f"(timeout={REQUEST_TIMEOUT_SECONDS:.1f}s)"
            ),
        )
    return {
        "status": "ok",
        "model": MODEL_ID,
        "device": "mps",
        "in_flight": _active_request_started_at is not None,
        "active_seconds": round(stalled_for, 3),
        "last_request_seconds": (
            round(_last_request_seconds, 3)
            if _last_request_seconds is not None
            else None
        ),
        "last_error": _last_error,
    }


@app.post("/embeddings", response_model=EmbeddingsResponse)
async def embeddings(req: EmbeddingsRequest) -> EmbeddingsResponse:
    global _active_request_started_at, _last_error, _last_request_seconds
    if _model is None:
        try:
            _load_model()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"model load failed: {exc}")

    inputs = [req.input] if isinstance(req.input, str) else list(req.input)
    if not inputs:
        raise HTTPException(status_code=400, detail="input is empty")

    acquired = False
    started = time.monotonic()
    try:
        try:
            await asyncio.wait_for(_request_gate.acquire(), timeout=QUEUE_TIMEOUT_SECONDS)
            acquired = True
        except TimeoutError:
            raise HTTPException(
                status_code=429,
                detail=(
                    "embedder is busy; previous request did not release within "
                    f"{QUEUE_TIMEOUT_SECONDS:.1f}s"
                ),
            )
        _active_request_started_at = time.monotonic()
        embeddings_np = await asyncio.wait_for(
            asyncio.to_thread(_encode_texts, inputs),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        _last_error = None
    except TimeoutError:
        _last_error = f"embedding request timed out after {REQUEST_TIMEOUT_SECONDS:.1f}s"
        logger.error("%s; exiting so launchd restarts the MLX sidecar", _last_error)
        os._exit(124)
    except HTTPException:
        raise
    except Exception as exc:
        _last_error = f"{type(exc).__name__}: {exc}"
        raise HTTPException(status_code=500, detail=f"embedding failed: {exc}")
    finally:
        _last_request_seconds = time.monotonic() - started
        _active_request_started_at = None
        if acquired:
            _request_gate.release()

    return EmbeddingsResponse(
        data=[
            EmbeddingItem(embedding=embeddings_np[i].tolist(), index=i)
            for i in range(len(inputs))
        ],
        model=MODEL_NAME,
    )

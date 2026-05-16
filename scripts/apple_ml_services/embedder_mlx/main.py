"""Apple Silicon MLX embedder sidecar — FastAPI, host-native.

Mounts an OpenAI-compatible /embeddings endpoint plus /info and /health,
matching the contract of the in-cluster embedder service so the rest of
Polymath doesn't notice the swap.

Wire spec (matches backend expectations):
  GET  /info       → model metadata + unified-memory telemetry
  GET  /health     → status + unified-memory telemetry
  POST /embeddings → OpenAI shape {data: [{embedding: [...], index: i}, ...]}

NOTE — IMPLEMENTATION SCAFFOLD
This file structure + endpoint shapes are correct. The actual MLX model
loading and forward-pass code below is a working starting point that you
should replace with your verified Mac Studio implementation if you've
already tuned batch size / pooling / normalization for your specific
quantization. Both implementations honor the same HTTP contract, so the
rest of the stack is unaffected by the swap.

Required env:
  EMBEDDER_PORT          default 8082 (set by start.sh)
  EMBED_MAX_LENGTH       default 512
  EMBED_BATCH_SIZE       default 8
  HF_HOME                provided by LaunchAgent
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("embedder_mlx")
logging.basicConfig(level=logging.INFO)

MODEL_ID = "mlx-community/Qwen3-Embedding-0.6B-mxfp8"
EMBED_DIM = 1024
MAX_LENGTH = int(os.environ.get("EMBED_MAX_LENGTH", "512"))
BATCH_SIZE = int(os.environ.get("EMBED_BATCH_SIZE", "8"))


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
_model: Any = None
_tokenizer: Any = None


def _memory_status() -> dict[str, Any]:
    try:
        import psutil

        mem = psutil.virtual_memory()
        available_mb = int(mem.available // (1024 * 1024))
        total_mb = int(mem.total // (1024 * 1024))
        used_percent = round(float(mem.percent), 1)
        if used_percent >= 92 or available_mb < 1024:
            pressure = "critical"
        elif used_percent >= 85 or available_mb < 2048:
            pressure = "high"
        elif used_percent >= 75:
            pressure = "moderate"
        else:
            pressure = "ok"
        return {
            # Names mirror the Docker CUDA sidecar contract. On Apple Silicon
            # this is unified system memory, not discrete VRAM.
            "gpu_free_mb": available_mb,
            "gpu_total_mb": total_mb,
            "memory_available_mb": available_mb,
            "memory_total_mb": total_mb,
            "memory_used_percent": used_percent,
            "memory_pressure": pressure,
        }
    except Exception as exc:
        logger.warning("memory telemetry unavailable: %s", exc)
        return {
            "gpu_free_mb": None,
            "gpu_total_mb": None,
            "memory_available_mb": None,
            "memory_total_mb": None,
            "memory_used_percent": None,
            "memory_pressure": "unknown",
        }


def _load_model() -> None:
    """Load Qwen3 embedding model via mlx-embeddings.

    The mlx-embeddings library is the lightweight wrapper around mlx that
    exposes pooled sentence embeddings for Qwen3 / BGE / E5 families.
    """
    global _model, _tokenizer
    if _model is not None:
        return
    try:
        from mlx_embeddings.utils import load
    except ImportError as exc:
        raise RuntimeError(
            "mlx-embeddings not installed. uv pip install -r requirements.txt"
        ) from exc

    logger.info("loading %s", MODEL_ID)
    _model, _tokenizer = load(MODEL_ID)
    logger.info("model ready (dim=%d, max_len=%d)", EMBED_DIM, MAX_LENGTH)


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
        "dimension": EMBED_DIM,
        "device": "mps",
        "max_length": MAX_LENGTH,
        "batch_size": BATCH_SIZE,
        "ready": _model is not None,
        **_memory_status(),
    }


@app.get("/health")
async def health() -> dict:
    memory = _memory_status()
    status = "ok" if _model is not None else "loading"
    if _model is not None and memory.get("memory_pressure") == "critical":
        status = "degraded"
    return {"status": status, **memory}


@app.post("/embeddings", response_model=EmbeddingsResponse)
async def embeddings(req: EmbeddingsRequest) -> EmbeddingsResponse:
    if _model is None:
        try:
            _load_model()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"model load failed: {exc}")

    inputs = [req.input] if isinstance(req.input, str) else list(req.input)
    if not inputs:
        raise HTTPException(status_code=400, detail="input is empty")

    # ── REPLACE THIS BLOCK with your verified Mac Studio implementation ──
    # The mlx-embeddings .generate / .encode API has shifted across
    # versions; the canonical pattern is:
    #     toks = _tokenizer(inputs, padding=True, truncation=True,
    #                       max_length=MAX_LENGTH, return_tensors="np")
    #     out  = _model(toks["input_ids"], attention_mask=toks["attention_mask"])
    #     emb  = out.text_embeds  # already L2-normalized for Qwen3
    # The exact attribute (pooler_output / sentence_embedding /
    # text_embeds) depends on the version of mlx-embeddings installed.
    # Keep the response shape identical to OpenAI so backend consumers
    # don't change.
    try:
        import numpy as np

        toks = _tokenizer(
            inputs,
            padding=True,
            truncation=True,
            max_length=MAX_LENGTH,
            return_tensors="np",
        )
        result = _model(toks["input_ids"], attention_mask=toks["attention_mask"])
        if hasattr(result, "text_embeds"):
            embeddings_np = np.asarray(result.text_embeds)
        elif hasattr(result, "sentence_embedding"):
            embeddings_np = np.asarray(result.sentence_embedding)
        else:
            raise RuntimeError(
                "Embedder returned an unrecognised output. Update embedder_mlx/main.py "
                "to extract the right attribute for your installed mlx-embeddings."
            )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"embedding failed: {exc}")

    return EmbeddingsResponse(
        data=[
            EmbeddingItem(embedding=embeddings_np[i].tolist(), index=i)
            for i in range(len(inputs))
        ],
        model=MODEL_ID,
    )

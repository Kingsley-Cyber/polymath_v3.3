"""Apple Silicon MLX reranker sidecar — Jina v3 cross-encoder, host-native.

Wire spec (matches backend expectations):
  GET  /health → {"status": "ok"}
  POST /rerank → {"scores": [float, ...]} aligned to the input documents

NOTE — IMPLEMENTATION SCAFFOLD
Jina-reranker-v3 ships as a Qwen3 trunk + a custom 2-layer MLP projector
that maps pooled embeddings to a single relevance scalar. Current
mlx-embeddings cannot load the projector's quantized weights
automatically, so the verified Mac Studio implementation builds an
MLPProjector by hand and loads only the trunk through mlx-embeddings.

This file gives you the FastAPI shape + a placeholder forward pass.
**Replace the _load_model() and _score_pairs() bodies with your
verified Mac Studio implementation** — the wire contract stays the same.

CRITICAL — score scale: this model returns COSINE scores (0..1), not
logits. The backend must run with RERANKER_SCORE_SCALE=cosine, otherwise
the negative-logit "low confidence" guard in the retriever throws away
every result. The docker-compose.apple-mlx.yml override sets this for you.

Required env:
  RERANKER_PORT             default 8081 (set by start.sh)
  RERANKER_BATCH_SIZE       default 16
  RERANKER_MAX_DOC_CHARS    default 6000
  RERANKER_MAX_QUERY_CHARS  default 2000
  HF_HOME                   provided by LaunchAgent
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("reranker_mlx")
logging.basicConfig(level=logging.INFO)

MODEL_ID = "mlx-community/jina-reranker-v3-4bit-mxfp4"
BATCH_SIZE = int(os.environ.get("RERANKER_BATCH_SIZE", "16"))
MAX_DOC_CHARS = int(os.environ.get("RERANKER_MAX_DOC_CHARS", "6000"))
MAX_QUERY_CHARS = int(os.environ.get("RERANKER_MAX_QUERY_CHARS", "2000"))


class RerankRequest(BaseModel):
    query: str
    documents: list[str]
    top_k: int | None = None


class RerankResponse(BaseModel):
    scores: list[float]
    model: str = MODEL_ID


app = FastAPI(title="Polymath Apple MLX Reranker (Jina v3)", version="0.1.0")
_model: Any = None
_projector: Any = None
_tokenizer: Any = None


def _load_model() -> None:
    """Load Jina v3 trunk + hand-built MLP projector.

    REPLACE THIS BODY with your verified Mac Studio implementation.
    The Mac Studio code:
      1. Loads the Qwen3 trunk via mlx_embeddings.utils.load(MODEL_ID).
      2. Reads model.safetensors metadata for the projector weights
         (keys typically: 'projector.0.weight', 'projector.0.bias',
                          'projector.2.weight', 'projector.2.bias').
      3. Constructs an MLPProjector(mlx.nn.Module) with shape
         [hidden_dim → hidden_dim → 1], loads the weights, freezes it.
      4. Stores both as module-level globals.
    """
    global _model, _projector, _tokenizer
    if _model is not None:
        return

    try:
        from mlx_embeddings.utils import load
    except ImportError as exc:
        raise RuntimeError(
            "mlx-embeddings not installed. uv pip install -r requirements.txt"
        ) from exc

    logger.info("loading Jina v3 trunk %s", MODEL_ID)
    _model, _tokenizer = load(MODEL_ID)

    # Projector — REPLACE with your hand-built MLPProjector load
    _projector = None
    logger.warning(
        "reranker scaffold active: MLPProjector not loaded. "
        "Replace _load_model() with the verified Mac Studio implementation. "
        "/rerank will return zeroes until then."
    )


@app.on_event("startup")
async def _startup() -> None:
    try:
        _load_model()
    except Exception as exc:
        logger.exception("startup model load failed: %s", exc)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok" if _model is not None else "loading"}


@app.get("/info")
async def info() -> dict:
    return {
        "model": MODEL_ID,
        "score_scale": "cosine",
        "batch_size": BATCH_SIZE,
        "max_doc_chars": MAX_DOC_CHARS,
        "max_query_chars": MAX_QUERY_CHARS,
        "ready": _model is not None and _projector is not None,
    }


def _score_pairs(query: str, documents: list[str]) -> list[float]:
    """Return cosine scores aligned to documents.

    REPLACE with your verified Mac Studio implementation. Reference shape:
      pairs = [(query[:MAX_QUERY_CHARS], d[:MAX_DOC_CHARS]) for d in documents]
      toks  = _tokenizer(pairs, padding=True, truncation=True, return_tensors="np")
      hidden = _model(toks["input_ids"], attention_mask=toks["attention_mask"]).last_hidden_state
      pooled = hidden[:, 0, :]                    # CLS pooling for Jina v3
      scores = _projector(pooled).reshape(-1)     # [N] cosine in 0..1
      return scores.tolist()
    """
    if _projector is None:
        # Scaffold mode: explicit zeroes signal misconfiguration to the
        # retriever rather than randomising relevance order.
        logger.warning("returning zero scores (scaffold mode)")
        return [0.0] * len(documents)

    try:
        import numpy as np

        pairs = [
            (query[:MAX_QUERY_CHARS], (doc or "")[:MAX_DOC_CHARS])
            for doc in documents
        ]
        toks = _tokenizer(
            pairs,
            padding=True,
            truncation=True,
            return_tensors="np",
        )
        out = _model(toks["input_ids"], attention_mask=toks["attention_mask"])
        # Pool the CLS token (index 0) — replace with the pooling your
        # verified implementation uses if different.
        hidden = np.asarray(out.last_hidden_state)
        pooled = hidden[:, 0, :]
        scores = _projector(pooled).reshape(-1)
        return [float(s) for s in scores]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"rerank failed: {exc}")


@app.post("/rerank", response_model=RerankResponse)
async def rerank(req: RerankRequest) -> RerankResponse:
    if not req.documents:
        return RerankResponse(scores=[])
    if _model is None:
        try:
            _load_model()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"model load failed: {exc}")
    scores = _score_pairs(req.query, req.documents)
    return RerankResponse(scores=scores)

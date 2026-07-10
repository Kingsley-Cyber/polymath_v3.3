"""Apple Silicon MLX reranker sidecar - Jina v3 cosine reranker, host-native.

Wire spec (matches backend expectations):
  GET  /health → {"status": "ok"}
  POST /rerank → {"scores": [float, ...]} aligned to the input documents

The MLX model card exposes Jina v3 through mlx-embeddings: generate
normalised text embeddings, then score query/document pairs with the dot
product. That gives cosine-like scores instead of cross-encoder logits.
The docker-compose.apple-mlx.yml override sets RERANKER_SCORE_SCALE=cosine
so the backend does not apply logit-only low-confidence guards.

Required env:
  APPLE_MLX_RERANKER_MODEL_ID default mlx-community/jina-reranker-v3-4bit-mxfp4
  RERANKER_PORT             default 8081 (set by start.sh)
  RERANKER_BATCH_SIZE       default 16
  RERANKER_MAX_DOC_CHARS    default 6000
  RERANKER_MAX_QUERY_CHARS  default 2000
  HF_HOME                   provided by LaunchAgent
"""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Any

import asyncio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("reranker_mlx")
logging.basicConfig(level=logging.INFO)

MODEL_ID = os.environ.get(
    "APPLE_MLX_RERANKER_MODEL_ID",
    os.environ.get("RERANKER_MODEL_ID", "mlx-community/jina-reranker-v3-4bit-mxfp4"),
)
# ── Retrieval Layer v4: TRUE cross-encoder backend ──────────────────────────
# The original "mlx" backend never ran a cross-encoder: it embeds query and
# documents separately through mlx-embeddings and scores by DOT PRODUCT —
# a bi-encoder cosine pass wearing a reranker's clothes. Measured margins on
# adversarial probes: ideal evidence 0.368 vs term-stuffed junk 0.308.
# backend "torch_fp16" runs jina-reranker-v3's actual LISTWISE cross-encoder
# head (fp16, PyTorch on MPS): same probe gives 0.437 vs -0.026 — an ~8x
# margin — and resists term-stuffing that fools pointwise rerankers.
# Raw listwise scores are mapped to [0,1] with a versioned calibration
# sigma((s - mu) / T) so the backend's bounded-score contract holds.
BACKEND = os.environ.get("APPLE_RERANKER_BACKEND", "mlx").strip().lower()
TORCH_MODEL_ID = os.environ.get(
    "APPLE_TORCH_RERANKER_MODEL_ID", "jinaai/jina-reranker-v3"
)
# calibration.v1-provisional: constants chosen from the 2026-07-02 probe set
# (real 0.437 -> 0.88, stuffed junk 0.02 -> 0.18, unrelated -0.125 -> 0.06).
# Refit on the golden set per RETRIEVAL_LAYER_SPEC.md and bump the version.
CAL_MU = float(os.environ.get("RERANKER_CAL_MU", "0.2"))
CAL_T = float(os.environ.get("RERANKER_CAL_T", "0.12"))
CAL_VERSION = os.environ.get("RERANKER_CAL_VERSION", "cal.v1-provisional")
BATCH_SIZE = int(os.environ.get("RERANKER_BATCH_SIZE", "16"))
MAX_DOC_CHARS = int(os.environ.get("RERANKER_MAX_DOC_CHARS", "6000"))
MAX_QUERY_CHARS = int(os.environ.get("RERANKER_MAX_QUERY_CHARS", "2000"))
REQUEST_TIMEOUT_SECONDS = float(os.environ.get("RERANKER_REQUEST_TIMEOUT_SECONDS", "60"))
QUEUE_TIMEOUT_SECONDS = float(os.environ.get("RERANKER_QUEUE_TIMEOUT_SECONDS", "5"))
WARM_ON_STARTUP = os.environ.get("RERANKER_WARM_ON_STARTUP", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


class RerankRequest(BaseModel):
    query: str
    documents: list[str]
    top_k: int | None = None


class RerankResponse(BaseModel):
    scores: list[float]
    model: str = TORCH_MODEL_ID if BACKEND == "torch_fp16" else MODEL_ID


app = FastAPI(title="Polymath Apple MLX Reranker (Jina v3)", version="0.1.0")
_model: Any = None
_tokenizer: Any = None
_generate: Any = None
_request_gate = asyncio.Semaphore(1)
_active_request_started_at: float | None = None
_last_request_seconds: float | None = None
_last_error: str | None = None
_warmup_complete = False
_warmup_seconds: float | None = None


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


_torch_model: Any = None


def _load_model_torch() -> None:
    """Load the fp16 listwise cross-encoder on MPS (torch backend)."""
    global _torch_model
    if _torch_model is not None:
        return
    import torch
    from transformers import AutoModel

    logger.info("loading TRUE cross-encoder %s (fp16, mps)", TORCH_MODEL_ID)
    _torch_model = (
        AutoModel.from_pretrained(
            TORCH_MODEL_ID, dtype=torch.float16, trust_remote_code=True
        )
        .to("mps")
        .eval()
    )
    logger.info(
        "torch reranker ready (calibration %s: mu=%.3f T=%.3f)",
        CAL_VERSION,
        CAL_MU,
        CAL_T,
    )


def _calibrate(raw: float) -> float:
    import math

    return 1.0 / (1.0 + math.exp(-(raw - CAL_MU) / CAL_T))


def _score_pairs_torch(query: str, documents: list[str]) -> list[float]:
    """One listwise cross-encoder pass; calibrated [0,1] scores aligned to
    the input document order."""
    query_text = (query or "")[:MAX_QUERY_CHARS]
    doc_texts = [(doc or "")[:MAX_DOC_CHARS] for doc in documents]
    results = _torch_model.rerank(query_text, doc_texts)
    by_index = {int(r["index"]): float(r["relevance_score"]) for r in results}
    return [
        _calibrate(by_index.get(i, -10.0)) for i in range(len(doc_texts))
    ]


def _load_model() -> None:
    """Load the MLX reranker model through mlx-embeddings."""
    global _generate, _model, _tokenizer
    if _model is not None:
        return

    load, generate = _import_mlx_embeddings()

    logger.info("loading Jina v3 MLX reranker %s", MODEL_ID)
    try:
        _model, _tokenizer = load(MODEL_ID)
    except ValueError as exc:
        message = str(exc)
        if "parameters not in model" not in message or "projector" not in message:
            raise
        logger.warning(
            "MLX loader rejected projector weights for %s; retrying with "
            "lenient weight loading",
            MODEL_ID,
        )
        import mlx.nn as nn

        original_load_weights = nn.Module.load_weights

        def _load_weights_lenient(
            self: Any, file_or_weights: Any, strict: bool = True
        ) -> Any:
            return original_load_weights(self, file_or_weights, strict=False)

        nn.Module.load_weights = _load_weights_lenient
        try:
            _model, _tokenizer = load(MODEL_ID)
        finally:
            nn.Module.load_weights = original_load_weights
    _generate = generate
    logger.info("reranker ready")


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
        "MLX reranker returned an unrecognised output; expected text_embeds, "
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


def _encode_batch(texts: list[str]) -> Any:
    if _model is None or _tokenizer is None:
        _load_model()

    if _generate is not None:
        try:
            output = _generate(_model, _tokenizer, texts=texts)
            return _normalise_rows(_extract_embeddings(output))
        except TypeError:
            output = _generate(_model, _tokenizer, texts)
            return _normalise_rows(_extract_embeddings(output))
        except Exception as exc:
            logger.warning("mlx-embeddings.generate failed; falling back to direct call: %s", exc)

    try:
        toks = _tokenizer(texts, padding=True, truncation=True, return_tensors="np")
        try:
            result = _model(toks["input_ids"], attention_mask=toks.get("attention_mask"))
        except TypeError:
            result = _model(toks["input_ids"])
        return _normalise_rows(_extract_embeddings(result))
    except Exception as exc:
        raise RuntimeError(f"reranker embedding failed: {exc}") from exc


def _encode_texts(texts: list[str]) -> Any:
    import numpy as np

    batches = []
    for start in range(0, len(texts), max(1, BATCH_SIZE)):
        batches.append(_encode_batch(texts[start : start + BATCH_SIZE]))
    return np.vstack(batches)


def _warm_model() -> None:
    """Load weights and execute a real inference before reporting ready."""
    global _last_error, _warmup_complete, _warmup_seconds
    started = time.monotonic()
    if BACKEND == "torch_fp16":
        _load_model_torch()
        scores = _score_pairs_torch(
            "retrieval readiness probe",
            ["This document verifies that the reranker inference path is ready."],
        )
    else:
        _load_model()
        scores = _score_pairs(
            "retrieval readiness probe",
            ["This document verifies that the reranker inference path is ready."],
        )
    if len(scores) != 1 or not math.isfinite(float(scores[0])):
        raise RuntimeError("reranker warmup returned an invalid score")
    _warmup_seconds = time.monotonic() - started
    _warmup_complete = True
    _last_error = None
    logger.info("reranker inference warmup complete in %.2fs", _warmup_seconds)


@app.on_event("startup")
async def _startup() -> None:
    global _last_error
    try:
        if WARM_ON_STARTUP:
            await asyncio.to_thread(_warm_model)
        elif BACKEND == "torch_fp16":
            await asyncio.to_thread(_load_model_torch)
        else:
            await asyncio.to_thread(_load_model)
    except Exception as exc:
        _last_error = f"startup warmup failed: {type(exc).__name__}: {exc}"
        logger.exception("startup model load failed: %s", exc)


@app.get("/health")
async def health() -> dict:
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
                f"rerank request stalled for {stalled_for:.1f}s "
                f"(timeout={REQUEST_TIMEOUT_SECONDS:.1f}s)"
            ),
        )
    if WARM_ON_STARTUP and not _warmup_complete:
        raise HTTPException(
            status_code=503,
            detail=_last_error or "model inference warmup is incomplete",
        )
    if BACKEND == "torch_fp16":
        if _torch_model is None:
            raise HTTPException(status_code=503, detail="model is not loaded")
        return {
            "status": "ok",
            "model": TORCH_MODEL_ID,
            "backend": "torch_fp16",
            "cross_encoder": True,
            "calibration": CAL_VERSION,
            "device": "mps",
            "in_flight": _active_request_started_at is not None,
            "active_seconds": round(stalled_for, 3),
            "last_request_seconds": (
                round(_last_request_seconds, 3)
                if _last_request_seconds is not None
                else None
            ),
            "last_error": _last_error,
            "warmup_complete": _warmup_complete,
            "warmup_seconds": (
                round(_warmup_seconds, 3) if _warmup_seconds is not None else None
            ),
        }
    if _model is None:
        raise HTTPException(status_code=503, detail="model is not loaded")
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
        "warmup_complete": _warmup_complete,
        "warmup_seconds": (
            round(_warmup_seconds, 3) if _warmup_seconds is not None else None
        ),
    }


@app.get("/info")
async def info() -> dict:
    if BACKEND == "torch_fp16":
        return {
            "model": TORCH_MODEL_ID,
            "backend": "torch_fp16",
            "score_scale": "probability",
            "calibration": {"version": CAL_VERSION, "mu": CAL_MU, "t": CAL_T},
            "max_doc_chars": MAX_DOC_CHARS,
            "max_query_chars": MAX_QUERY_CHARS,
            "ready": _torch_model is not None,
            "warmup_complete": _warmup_complete,
            "warmup_seconds": _warmup_seconds,
        }
    return {
        "model": MODEL_ID,
        "score_scale": "cosine",
        "batch_size": BATCH_SIZE,
        "max_doc_chars": MAX_DOC_CHARS,
        "max_query_chars": MAX_QUERY_CHARS,
        "ready": _model is not None,
        "warmup_complete": _warmup_complete,
        "warmup_seconds": _warmup_seconds,
    }


def _score_pairs(query: str, documents: list[str]) -> list[float]:
    """Return cosine scores aligned to documents."""
    try:
        query_text = (query or "")[:MAX_QUERY_CHARS]
        doc_texts = [(doc or "")[:MAX_DOC_CHARS] for doc in documents]
        vectors = _encode_texts([query_text] + doc_texts)
        query_vec = vectors[0]
        doc_vecs = vectors[1:]
        scores = doc_vecs @ query_vec
        return [float(score) for score in scores.tolist()]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"rerank failed: {exc}")


@app.post("/rerank", response_model=RerankResponse)
async def rerank(req: RerankRequest) -> RerankResponse:
    global _active_request_started_at, _last_error, _last_request_seconds
    if not req.documents:
        return RerankResponse(scores=[])
    if BACKEND == "torch_fp16":
        if _torch_model is None:
            try:
                _load_model_torch()
            except Exception as exc:
                raise HTTPException(
                    status_code=503, detail=f"model load failed: {exc}"
                )
        scorer = _score_pairs_torch
    else:
        if _model is None:
            try:
                _load_model()
            except Exception as exc:
                raise HTTPException(status_code=503, detail=f"model load failed: {exc}")
        scorer = _score_pairs

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
                    "reranker is busy; previous request did not release within "
                    f"{QUEUE_TIMEOUT_SECONDS:.1f}s"
                ),
            )
        _active_request_started_at = time.monotonic()
        scores = await asyncio.wait_for(
            asyncio.to_thread(scorer, req.query, req.documents),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        _last_error = None
    except TimeoutError:
        _last_error = f"rerank request timed out after {REQUEST_TIMEOUT_SECONDS:.1f}s"
        logger.error("%s; exiting so launchd restarts the MLX sidecar", _last_error)
        os._exit(124)
    except HTTPException:
        raise
    except Exception as exc:
        _last_error = f"{type(exc).__name__}: {exc}"
        raise HTTPException(status_code=500, detail=f"rerank failed: {exc}")
    finally:
        _last_request_seconds = time.monotonic() - started
        _active_request_started_at = None
        if acquired:
            _request_gate.release()
    return RerankResponse(scores=scores)

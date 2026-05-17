"""
Reranker service — sentence-transformers CrossEncoder sidecar.

Default model: Qwen/Qwen3-Reranker-0.6B. Override with RERANKER_MODEL or
MODEL_PATH. The wire API remains stable so backend retrieval logic keeps using
the same /rerank contract regardless of the loaded model.
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import List

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sentence_transformers import CrossEncoder

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEFAULT_RERANKER_MODEL = "Qwen/Qwen3-Reranker-0.6B"
MODEL_PATH = (
    os.getenv("RERANKER_MODEL") or os.getenv("MODEL_PATH") or DEFAULT_RERANKER_MODEL
)
REQUESTED_DEVICE = os.getenv("RERANKER_DEVICE", "auto").strip().lower()

model: CrossEncoder = None
active_device: str | None = None
last_self_test_error: str | None = None


def _candidate_devices() -> list[str]:
    if REQUESTED_DEVICE == "auto":
        return ["cuda", "cpu"] if torch.cuda.is_available() else ["cpu"]
    if REQUESTED_DEVICE == "cuda":
        return ["cuda", "cpu"]
    return [REQUESTED_DEVICE]


def _predict_health_probe(candidate: CrossEncoder) -> None:
    scores = candidate.predict([["health query", "health document"]])
    if len(scores) != 1:
        raise RuntimeError("reranker self-test returned an unexpected score shape")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, active_device, last_self_test_error
    errors: list[str] = []
    for device in _candidate_devices():
        logger.info(f"Loading cross-encoder from {MODEL_PATH} on {device}")
        try:
            candidate = CrossEncoder(MODEL_PATH, max_length=512, device=device)
            _predict_health_probe(candidate)
            model = candidate
            active_device = device
            last_self_test_error = None
            logger.info("Reranker model loaded successfully on %s", device)
            break
        except Exception as exc:
            last_self_test_error = str(exc)
            errors.append(f"{device}: {exc}")
            logger.exception(
                "Reranker failed on %s; trying fallback if available", device
            )
            if device == "cuda" and torch.cuda.is_available():
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    logger.debug("torch.cuda.empty_cache failed", exc_info=True)

    if model is None:
        raise RuntimeError("Reranker failed to start: " + " | ".join(errors))

    yield
    model = None
    active_device = None


app = FastAPI(title="Polymath Reranker", lifespan=lifespan)


class RerankRequest(BaseModel):
    query: str
    documents: List[str]
    top_n: int = None


class RankedResult(BaseModel):
    index: int
    score: float
    text: str


class RerankResponse(BaseModel):
    results: List[RankedResult]


@app.get("/health")
def health():
    """Pt 9b mirror — end-to-end probe including a real cross-encoder forward pass.

    The model object stays in Python memory after a CUDA context corruption
    (cudaErrorUnknown on WSL2 + WDDM / RTX PRO 6000 Blackwell). Without a
    real forward pass here, /health returns 200 while every /rerank request
    fails with 503. Docker's healthcheck sees green, autoheal never fires,
    and retrieval silently degrades to score-sort. Reuse the same probe
    helper the startup lifespan uses — fail fast if it raises so the
    healthcheck loop will flip unhealthy and autoheal can restart the
    container to clear the poisoned context.
    """
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    try:
        _predict_health_probe(model)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"GPU forward pass failed: {type(exc).__name__}: {exc}",
        )
    return {
        "status": "ok",
        "model": MODEL_PATH,
        "device": active_device,
        "requested_device": REQUESTED_DEVICE,
        "self_test": "ok",
    }


@app.get("/info")
def info():
    """Return model metadata so orchestrators can verify the loaded model
    and score scale without triggering a forward pass.
    """
    return {
        "model": MODEL_PATH,
        "score_scale": "cosine" if "cosine" in MODEL_PATH.lower() else "logit",
    }


@app.post("/rerank", response_model=RerankResponse)
def rerank(req: RerankRequest):
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    if not req.documents:
        return RerankResponse(results=[])

    pairs = [[req.query, doc] for doc in req.documents]
    try:
        scores = model.predict(pairs).tolist()
    except Exception as exc:
        logger.exception("Reranker prediction failed")
        raise HTTPException(
            status_code=503, detail=f"Reranker prediction failed: {exc}"
        ) from exc

    ranked = sorted(
        [
            {"index": i, "score": scores[i], "text": req.documents[i]}
            for i in range(len(req.documents))
        ],
        key=lambda x: x["score"],
        reverse=True,
    )

    if req.top_n:
        ranked = ranked[: req.top_n]

    return RerankResponse(results=[RankedResult(**r) for r in ranked])

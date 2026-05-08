"""
Reranker service — sentence-transformers cross-encoder
Replaces llama.cpp approach (incompatible with BERT cross-encoders)
Model: cross-encoder/ms-marco-MiniLM-L6-v2
"""

import os
import logging
from contextlib import asynccontextmanager
from typing import List

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sentence_transformers import CrossEncoder
import torch

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_PATH = os.getenv("MODEL_PATH", "/models/ms-marco-MiniLM-L6-v2")
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
            logger.exception("Reranker failed on %s; trying fallback if available", device)
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
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return {
        "status": "ok",
        "model": MODEL_PATH,
        "device": active_device,
        "requested_device": REQUESTED_DEVICE,
        "self_test": "ok",
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
        raise HTTPException(status_code=503, detail=f"Reranker prediction failed: {exc}") from exc

    ranked = sorted(
        [{"index": i, "score": scores[i], "text": req.documents[i]} for i in range(len(req.documents))],
        key=lambda x: x["score"],
        reverse=True,
    )

    if req.top_n:
        ranked = ranked[: req.top_n]

    return RerankResponse(results=[RankedResult(**r) for r in ranked])

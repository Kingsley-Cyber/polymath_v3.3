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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MODEL_PATH = os.getenv("MODEL_PATH", "/models/ms-marco-MiniLM-L6-v2")
DEVICE = os.getenv("RERANKER_DEVICE", "cuda")

model: CrossEncoder = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model
    logger.info(f"Loading cross-encoder from {MODEL_PATH} on {DEVICE}")
    model = CrossEncoder(MODEL_PATH, max_length=512, device=DEVICE)
    logger.info("Reranker model loaded successfully")
    yield
    model = None


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
    return {"status": "ok", "model": MODEL_PATH}


@app.post("/rerank", response_model=RerankResponse)
def rerank(req: RerankRequest):
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    if not req.documents:
        return RerankResponse(results=[])

    pairs = [[req.query, doc] for doc in req.documents]
    scores = model.predict(pairs).tolist()

    ranked = sorted(
        [{"index": i, "score": scores[i], "text": req.documents[i]} for i in range(len(req.documents))],
        key=lambda x: x["score"],
        reverse=True,
    )

    if req.top_n:
        ranked = ranked[: req.top_n]

    return RerankResponse(results=[RankedResult(**r) for r in ranked])

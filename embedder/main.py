"""
Polymath Embedder Service
Loads any HuggingFace sentence-transformers model from MODEL_PATH.
Exposes OpenAI-compatible /embeddings endpoint + /health + /info.
No hardcoded model names, paths, or dimensions — all introspected at startup.
"""

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Config from env — never hardcoded ─────────────────────────────────────────
MODEL_PATH = os.getenv("MODEL_PATH", "/model")
MODEL_NAME = os.getenv("MODEL_NAME", Path(MODEL_PATH).name)
BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "32"))
DEVICE = os.getenv("EMBED_DEVICE", "cuda")  # cuda | cpu — override via env

# ── Runtime state ──────────────────────────────────────────────────────────────
model: SentenceTransformer = None
embedding_dim: int = None
model_name: str = None
encode_lock = threading.Lock()


def _gpu_memory() -> dict[str, int | None]:
    if DEVICE != "cuda":
        return {"gpu_free_mb": None, "gpu_total_mb": None}
    try:
        import torch

        free, total = torch.cuda.mem_get_info()
        return {
            "gpu_free_mb": int(free // (1024 * 1024)),
            "gpu_total_mb": int(total // (1024 * 1024)),
        }
    except Exception:
        return {"gpu_free_mb": None, "gpu_total_mb": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, embedding_dim, model_name

    logger.info(f"Loading model from {MODEL_PATH} on device={DEVICE}")
    start = time.time()

    model = SentenceTransformer(MODEL_PATH, device=DEVICE)

    # Introspect dimension — works for any sentence-transformers model
    embedding_dim = model.get_sentence_embedding_dimension()
    model_name = MODEL_NAME

    elapsed = time.time() - start
    logger.info(f"Model loaded: name={model_name} dim={embedding_dim} device={DEVICE} in {elapsed:.1f}s")

    yield

    model = None
    embedding_dim = None


app = FastAPI(title="Polymath Embedder", lifespan=lifespan)


# ── Schemas ────────────────────────────────────────────────────────────────────

class EmbeddingRequest(BaseModel):
    input: Union[str, list[str]]
    model: str = None  # accepted but ignored — model is fixed to what's loaded


class EmbeddingObject(BaseModel):
    object: str = "embedding"
    index: int
    embedding: list[float]


class EmbeddingUsage(BaseModel):
    prompt_tokens: int
    total_tokens: int


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingObject]
    model: str
    usage: EmbeddingUsage


class InfoResponse(BaseModel):
    model_name: str
    model_path: str
    dimension: int
    device: str
    batch_size: int
    gpu_free_mb: int | None = None
    gpu_total_mb: int | None = None


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return {
        "status": "ok",
        "model": model_name,
        "dimension": embedding_dim,
        "device": DEVICE,
        "batch_size": BATCH_SIZE,
        **_gpu_memory(),
    }


@app.get("/info", response_model=InfoResponse)
def info():
    """Returns model metadata — used by backend model discovery."""
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")
    return InfoResponse(
        model_name=model_name,
        model_path=MODEL_PATH,
        dimension=embedding_dim,
        device=DEVICE,
        batch_size=BATCH_SIZE,
        **_gpu_memory(),
    )


@app.post("/embeddings", response_model=EmbeddingResponse)
def embed(req: EmbeddingRequest):
    """
    OpenAI-compatible embeddings endpoint.
    Accepts single string or list of strings.
    Returns list of embedding vectors with usage stats.
    """
    if model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    # Normalize input to list
    texts = [req.input] if isinstance(req.input, str) else req.input

    if not texts:
        raise HTTPException(status_code=400, detail="input must be non-empty")

    # Encode — batch_size controls GPU memory pressure
    # Serialize local GPU encodes inside this process. Large ingestion batches
    # already split by BATCH_SIZE; concurrent encode calls are what can spike
    # VRAM and make the user's desktop unusable.
    with encode_lock:
        vectors = model.encode(
            texts,
            batch_size=BATCH_SIZE,
            normalize_embeddings=True,  # cosine similarity ready
            show_progress_bar=False,
        )

    data = [
        EmbeddingObject(index=i, embedding=vec.tolist())
        for i, vec in enumerate(vectors)
    ]

    # Approximate token count — sentence-transformers doesn't expose exact count
    approx_tokens = sum(len(t.split()) for t in texts)

    return EmbeddingResponse(
        data=data,
        model=model_name,
        usage=EmbeddingUsage(
            prompt_tokens=approx_tokens,
            total_tokens=approx_tokens,
        ),
    )

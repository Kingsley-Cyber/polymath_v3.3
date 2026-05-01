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
import gc
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
MODEL_ID = os.getenv("MODEL_ID", "")
MODEL_NAME = os.getenv("MODEL_NAME", MODEL_ID or Path(MODEL_PATH).name)
BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "32"))
DEVICE = os.getenv("EMBED_DEVICE", "cuda")  # cuda | cpu — override via env
MAX_SEQ_LENGTH = int(os.getenv("EMBED_MAX_SEQ_LENGTH", "1024"))
MAX_INPUT_CHARS = int(os.getenv("EMBED_MAX_INPUT_CHARS", "6000"))

# ── Runtime state ──────────────────────────────────────────────────────────────
model: SentenceTransformer = None
embedding_dim: int = None
model_name: str = None
model_source: str = None
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


def _model_source() -> str:
    """Prefer a populated local model mount, otherwise use a HF model id.

    Docker creates bind-mount directories even when the expected model files
    are missing. Passing that empty directory into SentenceTransformer fails
    late with a huge transformers error. This makes the failure mode sane and
    lets the service warm the persisted Hugging Face cache on first boot.
    """
    path = Path(MODEL_PATH)
    if path.exists() and (
        (path / "modules.json").exists()
        or (path / "config.json").exists()
        or (path / "sentence_bert_config.json").exists()
    ):
        return MODEL_PATH
    return MODEL_ID or MODEL_NAME or MODEL_PATH


def _load_sentence_model(source: str) -> SentenceTransformer:
    loaded = SentenceTransformer(source, device=DEVICE)
    if MAX_SEQ_LENGTH > 0 and hasattr(loaded, "max_seq_length"):
        loaded.max_seq_length = MAX_SEQ_LENGTH
        logger.info("Model max_seq_length set to %d", MAX_SEQ_LENGTH)
    return loaded


def _trim_text(text: str) -> str:
    if MAX_INPUT_CHARS > 0 and len(text) > MAX_INPUT_CHARS:
        return text[:MAX_INPUT_CHARS]
    return text


def _is_cuda_runtime_error(message: str) -> bool:
    lower = message.lower()
    return (
        "out of memory" in lower
        or "cuda" in lower
        or "cudacachingallocator" in lower
        or "c10/cuda" in lower
    )


def _clear_cuda_cache():
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        logger.exception("Failed to clear CUDA cache after embedder CUDA error")


def _reset_model_after_cuda_error():
    global model, embedding_dim, model_name, model_source

    old_model = model
    model = None
    del old_model
    gc.collect()
    _clear_cuda_cache()

    source = model_source or _model_source()
    logger.warning("Reloading embedder model after CUDA runtime error")
    model = _load_sentence_model(source)
    embedding_dim = model.get_sentence_embedding_dimension()
    model_name = MODEL_NAME


@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, embedding_dim, model_name, model_source

    model_source = _model_source()
    logger.info(f"Loading model from {model_source} on device={DEVICE}")
    start = time.time()

    model = _load_sentence_model(model_source)

    # Introspect dimension — works for any sentence-transformers model
    embedding_dim = model.get_sentence_embedding_dimension()
    model_name = MODEL_NAME

    elapsed = time.time() - start
    logger.info(f"Model loaded: name={model_name} source={model_source} dim={embedding_dim} device={DEVICE} in {elapsed:.1f}s")

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
    texts = [_trim_text(str(text)) for text in texts]

    if not texts:
        raise HTTPException(status_code=400, detail="input must be non-empty")

    # Encode — batch_size controls GPU memory pressure
    # Serialize local GPU encodes inside this process. Large ingestion batches
    # already split by BATCH_SIZE; concurrent encode calls are what can spike
    # VRAM and make the user's desktop unusable.
    try:
        with encode_lock:
            vectors = model.encode(
                texts,
                batch_size=BATCH_SIZE,
                normalize_embeddings=True,  # cosine similarity ready
                show_progress_bar=False,
            )
    except RuntimeError as exc:
        message = str(exc)
        if _is_cuda_runtime_error(message):
            try:
                _reset_model_after_cuda_error()
            except Exception:
                logger.exception("Failed to reload embedder model after CUDA runtime error")
            logger.warning(
                "Embedder CUDA runtime error for request_size=%d batch_size=%d; caller should retry smaller",
                len(texts),
                BATCH_SIZE,
            )
            raise HTTPException(
                status_code=503,
                detail="cuda_runtime_error; retry with a smaller embedding batch",
            ) from exc
        raise

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

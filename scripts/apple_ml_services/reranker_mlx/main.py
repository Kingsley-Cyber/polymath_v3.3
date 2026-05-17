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
import os
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("reranker_mlx")
logging.basicConfig(level=logging.INFO)

MODEL_ID = os.environ.get(
    "APPLE_MLX_RERANKER_MODEL_ID",
    os.environ.get("RERANKER_MODEL_ID", "mlx-community/jina-reranker-v3-4bit-mxfp4"),
)
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
    """Load the MLX reranker model through mlx-embeddings."""
    global _generate, _model, _tokenizer
    if _model is not None:
        return

    load, generate = _import_mlx_embeddings()

    logger.info("loading Jina v3 MLX reranker %s", MODEL_ID)
    _model, _tokenizer = load(MODEL_ID)
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


@app.on_event("startup")
async def _startup() -> None:
    try:
        _load_model()
    except Exception as exc:
        logger.exception("startup model load failed: %s", exc)


@app.get("/health")
async def health() -> dict:
    if _model is None:
        raise HTTPException(status_code=503, detail="model is not loaded")
    return {"status": "ok", "model": MODEL_ID, "device": "mps"}


@app.get("/info")
async def info() -> dict:
    return {
        "model": MODEL_ID,
        "score_scale": "cosine",
        "batch_size": BATCH_SIZE,
        "max_doc_chars": MAX_DOC_CHARS,
        "max_query_chars": MAX_QUERY_CHARS,
        "ready": _model is not None,
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
    if not req.documents:
        return RerankResponse(scores=[])
    if _model is None:
        try:
            _load_model()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"model load failed: {exc}")
    scores = _score_pairs(req.query, req.documents)
    return RerankResponse(scores=scores)

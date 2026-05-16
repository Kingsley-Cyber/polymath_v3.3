"""Apple Silicon MLX reranker sidecar — Jina v3, host-native.

Wire spec (matches backend expectations):
  GET  /health → status + unified-memory telemetry
  GET  /info   → readiness + score scale + unified-memory telemetry
  POST /rerank → {"results": [{"index", "score", "text"}, ...]}

This wrapper uses Jina's official MLX implementation from
`jinaai/jina-reranker-v3-mlx`, including its projector.safetensors loader.
It deliberately does not return placeholder zero scores. If the official
reranker cannot load, `/info.ready` is false and `/rerank` returns 503 so
the installer smoke test catches the issue before real retrieval traffic.

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

import importlib.util
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("reranker_mlx")
logging.basicConfig(level=logging.INFO)

MODEL_ID = "jinaai/jina-reranker-v3-mlx"
BATCH_SIZE = int(os.environ.get("RERANKER_BATCH_SIZE", "16"))
MAX_DOC_CHARS = int(os.environ.get("RERANKER_MAX_DOC_CHARS", "6000"))
MAX_QUERY_CHARS = int(os.environ.get("RERANKER_MAX_QUERY_CHARS", "2000"))
ALLOW_DOWNLOADS = os.environ.get("RERANKER_ALLOW_DOWNLOADS", "1").lower() not in {
    "0",
    "false",
    "no",
}


class RerankRequest(BaseModel):
    query: str
    documents: list[str]
    top_k: int | None = None


class RankedResult(BaseModel):
    index: int
    score: float
    text: str


class RerankResponse(BaseModel):
    results: list[RankedResult]
    model: str = MODEL_ID


app = FastAPI(title="Polymath Apple MLX Reranker (Jina v3)", version="0.1.0")
_reranker: Any = None
_model_dir: str | None = None
_load_error: str | None = None


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


def _resolve_model_dir() -> Path:
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "huggingface_hub not installed. uv pip install -r requirements.txt"
        ) from exc

    cache_dir = os.environ.get("HF_HOME")
    return Path(
        snapshot_download(
            repo_id=MODEL_ID,
            cache_dir=cache_dir,
            local_files_only=not ALLOW_DOWNLOADS,
            allow_patterns=[
                "*.json",
                "*.txt",
                "*.safetensors",
                "rerank.py",
                "tokenizer*",
                "merges.txt",
                "vocab.json",
            ],
        )
    )


def _load_mlx_reranker_class(model_dir: Path) -> Any:
    module_path = model_dir / "rerank.py"
    if not module_path.exists():
        raise RuntimeError(f"official rerank.py missing from {model_dir}")

    spec = importlib.util.spec_from_file_location(
        "jina_reranker_v3_mlx_official",
        module_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not import official rerank.py from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reranker_cls = getattr(module, "MLXReranker", None)
    if reranker_cls is None:
        raise RuntimeError("official rerank.py does not expose MLXReranker")
    return reranker_cls


def _load_model() -> None:
    """Load Jina's official MLX reranker implementation."""
    global _reranker, _model_dir, _load_error
    if _reranker is not None:
        return

    logger.info("resolving Jina v3 MLX reranker %s", MODEL_ID)
    model_dir = _resolve_model_dir()
    projector_path = model_dir / "projector.safetensors"
    if not projector_path.exists():
        raise RuntimeError(f"projector.safetensors missing from {model_dir}")

    reranker_cls = _load_mlx_reranker_class(model_dir)
    logger.info("loading Jina v3 MLX reranker from %s", model_dir)
    _reranker = reranker_cls(
        model_path=str(model_dir),
        projector_path=str(projector_path),
    )
    _model_dir = str(model_dir)
    _load_error = None


@app.on_event("startup")
async def _startup() -> None:
    try:
        _load_model()
    except Exception as exc:
        global _load_error
        _load_error = str(exc)
        logger.exception("startup model load failed: %s", exc)


@app.get("/health")
async def health() -> dict:
    memory = _memory_status()
    status = "ok" if _reranker is not None else "loading"
    if _load_error and _reranker is None:
        status = "error"
    if _reranker is not None and memory.get("memory_pressure") == "critical":
        status = "degraded"
    return {"status": status, "ready": _reranker is not None, **memory}


@app.get("/info")
async def info() -> dict:
    return {
        "model": MODEL_ID,
        "score_scale": "cosine",
        "batch_size": BATCH_SIZE,
        "max_doc_chars": MAX_DOC_CHARS,
        "max_query_chars": MAX_QUERY_CHARS,
        "ready": _reranker is not None,
        "model_dir": _model_dir,
        "load_error": _load_error,
        **_memory_status(),
    }


def _rank_pairs(query: str, documents: list[str], top_k: int | None) -> list[RankedResult]:
    if _reranker is None:
        raise HTTPException(status_code=503, detail="reranker is not ready")
    try:
        safe_query = (query or "")[:MAX_QUERY_CHARS]
        safe_documents = [(doc or "")[:MAX_DOC_CHARS] for doc in documents]
        raw_results = _reranker.rerank(
            safe_query,
            safe_documents,
            top_n=top_k,
        )
        return [
            RankedResult(
                index=int(item["index"]),
                score=float(item["relevance_score"]),
                text=documents[int(item["index"])],
            )
            for item in raw_results
        ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"rerank failed: {exc}")


@app.post("/rerank", response_model=RerankResponse)
async def rerank(req: RerankRequest) -> RerankResponse:
    if not req.documents:
        return RerankResponse(results=[])
    if _reranker is None:
        try:
            _load_model()
        except Exception as exc:
            global _load_error
            _load_error = str(exc)
            raise HTTPException(status_code=503, detail=f"model load failed: {exc}")
    results = _rank_pairs(req.query, req.documents, req.top_k)
    return RerankResponse(results=results)

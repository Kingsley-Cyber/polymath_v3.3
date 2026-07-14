"""Runpod Flash worker for Polymath burst embedding (bulk/backfill lane).

Deploy from this directory with ``flash deploy``. The worker is deliberately
stateless: it accepts a bounded list of texts and returns vectors. Database
credentials and write access never leave the Polymath backend.

EMBEDDING COMPATIBILITY INVARIANT
─────────────────────────────────
Vectors produced here MUST be interchangeable with vectors produced by the
local embedder sidecar (``embedder/main.py``) and the Modal reference
deployment (``modal_embedder.py``), because they land in the same Qdrant
collections. That means, exactly:

  - same model:         ``Qwen/Qwen3-Embedding-0.6B`` (override the
                        ``POLYMATH_EMBED_MODEL_ID`` deploy env only in
                        lockstep with every other embedding host and the
                        corpus's frozen ``embedding_model_id``),
  - same pooling:       whatever the model repository's sentence-transformers
                        pooling config ships (last-token pooling for
                        Qwen3-Embedding) — never overridden here,
  - same normalization: ``normalize_embeddings=True`` (unit L2 norm), the
                        exact ``model.encode`` call both reference hosts use,
  - no worker-side prompt mutation: the backend dispatcher owns any
                        model/role serialization and sends provider-ready
                        strings; this worker encodes those bytes unchanged,
  - same dimension:     1024 for the default model. The backend dispatcher
                        (``backend/services/embedder.py``) asserts the
                        dimension of every returned vector and raises on
                        mismatch — it never truncates or pads.

Wire contract (module constant, mirrored in ``backend/services/embedder.py``):

    request  {"contract_version": "polymath.runpod_embed.v1",
              "texts": ["...", ...]}                # <= 256 texts
    response {"contract_version": "polymath.runpod_embed.v1",
              "vectors": [[...], ...],              # len == len(texts)
              "model": "<model id>", "dim": 1024,
              "metrics": {...}}                     # additive diagnostics

Testability note: both the ``runpod_flash`` SDK import and the model load are
lazy/guarded so this module imports inside the Polymath backend container
(which ships neither ``runpod_flash`` nor ``torch``) and the offline test
suite can duck-type the model. ``flash deploy`` always runs with the real
SDK installed, so the stub below is test-only by construction.
"""

from __future__ import annotations

import os
import time
from enum import Enum
from typing import Any

try:  # pragma: no cover - exercised implicitly by the deploy environment
    from runpod_flash import Endpoint, GpuType, ServerlessScalerType

    _RUNPOD_FLASH_AVAILABLE = True
except ModuleNotFoundError:  # test environments without the flash SDK
    _RUNPOD_FLASH_AVAILABLE = False

    class GpuType(str, Enum):  # type: ignore[no-redef]
        """Test stub mirroring the runpod_flash GPU identifiers we use."""

        NVIDIA_L4 = "NVIDIA L4"
        NVIDIA_RTX_A5000 = "NVIDIA RTX A5000"
        NVIDIA_GEFORCE_RTX_4090 = "NVIDIA GeForce RTX 4090"

    class ServerlessScalerType(str, Enum):  # type: ignore[no-redef]
        REQUEST_COUNT = "REQUEST_COUNT"

    def Endpoint(**config: Any):  # type: ignore[no-redef]
        """Test stub: record the endpoint config instead of registering it."""

        def _wrap(func):
            func.__stub_endpoint_config__ = config
            return func

        return _wrap


_CONTRACT_VERSION = "polymath.runpod_embed.v1"
_ACCEPTED_CONTRACT_VERSIONS = frozenset({_CONTRACT_VERSION})
# Hard per-request cap. The backend dispatcher slices its workload into
# <=256-text requests; anything larger is rejected loudly rather than
# silently encoded (huge requests hide latency and wedge burst workers).
_MAX_TEXTS_PER_REQUEST = 256

_MODEL_ID = os.getenv("POLYMATH_EMBED_MODEL_ID", "Qwen/Qwen3-Embedding-0.6B")
# GPU-internal encode micro-batch, matching the local sidecar / Modal hosts.
_ENCODE_BATCH_SIZE = int(os.getenv("POLYMATH_EMBED_ENCODE_BATCH_SIZE", "32"))

# Loaded once per worker process and kept warm across requests (module
# global, same pattern as runpod_flash_extractor's _MODEL_CACHE).
_MODEL: Any | None = None


def _load_model() -> Any:
    """Heavy import + load, split out so tests can monkeypatch it."""
    import torch
    from sentence_transformers import SentenceTransformer

    device = "cuda" if torch.cuda.is_available() else "cpu"
    started = time.perf_counter()
    model = SentenceTransformer(_MODEL_ID, device=device)
    print(
        f"[runpod-embed] loaded {_MODEL_ID} "
        f"dim={model.get_sentence_embedding_dimension()} device={device} "
        f"in {time.perf_counter() - started:.1f}s"
    )
    return model


def _model() -> Any:
    global _MODEL
    if _MODEL is None:
        _MODEL = _load_model()
    return _MODEL


def _handle_embed_request(payload: dict[str, Any]) -> dict[str, Any]:
    """Pure request handler — validation, encode, response envelope.

    Kept separate from the ``@Endpoint``-decorated entrypoint so offline
    tests exercise the full contract without the flash runtime.
    """
    started = time.perf_counter()
    if payload.get("contract_version") not in _ACCEPTED_CONTRACT_VERSIONS:
        raise ValueError("unsupported embed contract")
    texts = payload.get("texts")
    if (
        not isinstance(texts, list)
        or not texts
        or not all(isinstance(text, str) for text in texts)
    ):
        raise ValueError("texts must be a non-empty list of strings")
    if len(texts) > _MAX_TEXTS_PER_REQUEST:
        raise ValueError(
            f"texts per request capped at {_MAX_TEXTS_PER_REQUEST}; "
            f"got {len(texts)}"
        )

    model = _model()
    # EXACT reference encode call — see the module-docstring invariant.
    vectors = model.encode(
        texts,
        batch_size=_ENCODE_BATCH_SIZE,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    # list(map(float, ...)) handles both numpy rows and plain-list fakes.
    rows = [list(map(float, vector)) for vector in vectors]
    dim = int(model.get_sentence_embedding_dimension())

    # COUNT AND DIMENSION ARE A CONTRACT (mirrors the backend's checks so a
    # broken worker fails on its own side of the wire too).
    if len(rows) != len(texts):
        raise ValueError(
            f"model returned {len(rows)} vectors for {len(texts)} texts"
        )
    for row in rows:
        if len(row) != dim:
            raise ValueError(
                f"model returned a {len(row)}-dim vector; expected {dim}"
            )

    duration = time.perf_counter() - started
    return {
        "contract_version": _CONTRACT_VERSION,
        "vectors": rows,
        "model": _MODEL_ID,
        "dim": dim,
        "metrics": {
            "texts": len(texts),
            "duration_seconds": round(duration, 4),
            "texts_per_second": round(len(texts) / duration, 3)
            if duration
            else 0.0,
        },
    }


@Endpoint(
    name=os.getenv("RUNPOD_EMBED_ENDPOINT_NAME", "polymath-embed-qwen3"),
    # GPU preference identical to runpod_flash_extractor/app.py.
    gpu=[
        GpuType.NVIDIA_L4,
        GpuType.NVIDIA_RTX_A5000,
        GpuType.NVIDIA_GEFORCE_RTX_4090,
    ],
    workers=(
        int(os.getenv("RUNPOD_EMBED_MIN_WORKERS", "0")),
        int(os.getenv("RUNPOD_EMBED_MAX_WORKERS", "8")),
    ),
    max_concurrency=int(os.getenv("RUNPOD_EMBED_WORKER_CONCURRENCY", "1")),
    idle_timeout=int(os.getenv("RUNPOD_EMBED_IDLE_TIMEOUT", "60")),
    scaler_type=ServerlessScalerType.REQUEST_COUNT,
    scaler_value=int(os.getenv("RUNPOD_EMBED_SCALER_VALUE", "1")),
    # 10 minutes, not the extractor's 30: an embed request is bounded at 256
    # texts (seconds of GPU time) plus a cold model download. Long execution
    # ceilings turn wedged workers into hidden multi-minute stalls.
    execution_timeout_ms=int(
        os.getenv("RUNPOD_EMBED_EXECUTION_TIMEOUT_MS", "600000")
    ),
    flashboot=True,
    accelerate_downloads=True,
    dependencies=[
        "torch>=2.4",
        # Qwen3-Embedding requires transformers>=4.51; sentence-transformers
        # >=2.7 per the model card.
        "transformers>=4.51,<5.0",
        "sentence-transformers>=2.7,<6.0",
        "numpy<2.3",
    ],
)
def embed_texts(payload: dict[str, Any]) -> dict[str, Any]:
    # Flash's generated handler calls ``embed_texts(**job_input)``; the
    # backend therefore submits ``{"input": {"payload": request}}`` (same
    # envelope rule as the extraction worker).
    return _handle_embed_request(payload)

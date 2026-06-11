"""ghost_b_extract_svc — native sidecar for fully-local Ghost B extraction.

Runs the deterministic GLiNER ×2 + GLiREL + Python-rules pipeline
(backend/services/ghost_b_local._extract_raw) on Apple Silicon MPS and serves
it over HTTP, so the Linux Docker backend (which has no Metal and no torch) can
keep its container topology and call extraction the same way it calls the
embedder (:8082) and docling (:8500) sidecars.

Port: 8084 (default). Launch via scripts/apple_ml_services/start.sh with
START_GHOST_B_EXTRACT=true, or manually:

    cd scripts/apple_ml_services
    ../../local_ghost_b/.venv/bin/python -m uvicorn \
        ghost_b_extract_svc.main:app --host 0.0.0.0 --port 8084

VENV: this service runs on local_ghost_b/.venv (NOT the shared apple_ml_services
.venv) — that venv carries the proven pinned ML set (torch MPS, gliner, glirel,
huggingface_hub<1.0, transformers<5) plus pydantic/fastapi/uvicorn. Re-creating
those pins elsewhere is exactly the dependency fight we already fought once.

Endpoints:
    GET  /health   {status, pipeline_version, warm, device}
    POST /extract  {tasks: [{chunk_id, doc_id, corpus_id, text}],
                    enable_facts: bool = true, schema_lens_id: str|null}
                -> {results: [ExtractionResult-shaped dicts], metrics: {...}}

The result dicts are the validated wire format defined by ghost_b_local:
entities/relations/facts have already passed LLMEntity/LLMRelation/LLMFact
validation; the backend client (ghost_b_local._to_results) turns them into the
ExtractionResult dataclasses. Deterministic: same tasks -> same response.

Env:
    GHOST_B_EXTRACT_WARM   default true — load GLiNER+GLiREL at startup in a
                           background thread (first /extract otherwise pays
                           ~20 s cold load).
    GHOST_B_GLINER_ONNX    "1" swaps the GLiNER forward (both passes) onto
                           ONNX Runtime — see pipeline_config for the repo /
                           file / device companions (GHOST_B_GLINER_ONNX_*).
                           /health then reports the ACTIVE ORT providers under
                           "gliner" — check it (plus nvidia-smi under load)
                           before trusting any CUDA bench number.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

# Make backend/ importable (services.ghost_b_local and friends). The module
# itself puts local_ghost_b/ + tools/ on the path at call time.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_BACKEND = _REPO_ROOT / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI, HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from services.ghost_b_local import _extract_raw, _metrics  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("ghost_b_extract_svc")

app = FastAPI(title="ghost_b_extract_svc", version="1.0")

_WARM = {"done": False, "error": ""}


class TaskIn(BaseModel):
    chunk_id: str = ""
    doc_id: str = ""
    corpus_id: str = ""
    text: str = ""
    # Table routing: kind selects the deterministic table-fact path; columns
    # is the linearizer's header list (slimmed from chunk metadata).
    chunk_kind: str = "body"
    columns: list[str] = Field(default_factory=list)


class ExtractIn(BaseModel):
    tasks: list[TaskIn] = Field(default_factory=list)
    enable_facts: bool = True
    schema_lens_id: str | None = None


def _warm_models() -> None:
    """Load GLiNER + GLiREL once so the first real request is warm."""
    try:
        _extract_raw(
            [{"chunk_id": "warmup", "doc_id": "warmup", "corpus_id": "warmup",
              "text": "Flame is a game engine built on Flutter."}],
            True, None,
        )
        _WARM["done"] = True
        logger.info("warmup complete — models resident")
    except Exception as exc:  # noqa: BLE001
        _WARM["error"] = str(exc)
        logger.exception("warmup failed")


@app.on_event("startup")
def _startup() -> None:
    if (os.environ.get("GHOST_B_EXTRACT_WARM", "true").strip().lower()
            in ("1", "true", "yes", "on")):
        threading.Thread(target=_warm_models, name="ghost-b-warmup", daemon=True).start()
    else:
        logger.info("warmup disabled (GHOST_B_EXTRACT_WARM=false)")


@app.get("/healthz")  # k8s-style alias — kills 404 noise from generic probes
@app.get("/health")
def health() -> dict:
    try:
        import torch
        if torch.cuda.is_available():
            device = f"cuda ({torch.cuda.get_device_name(0)})"
        elif (getattr(torch.backends, "mps", None)
              and torch.backends.mps.is_available()):
            device = "mps"
        else:
            device = "cpu"
    except Exception:  # noqa: BLE001
        device = "unavailable"
    try:
        from services.ghost_b_local import _ensure_local_ghost_b_on_path
        version = _ensure_local_ghost_b_on_path().PIPELINE_VERSION
    except Exception:  # noqa: BLE001
        version = "unknown"
    try:
        from services.ingestion.facet_tagger import gliner_backend_info
        gliner = gliner_backend_info()
    except Exception as exc:  # noqa: BLE001
        gliner = {"introspect_error": str(exc)}
    return {
        "status": "ok",
        "service": "ghost_b_extract",
        "pipeline_version": version,
        "warm": _WARM["done"],
        "warm_error": _WARM["error"],
        "device": device,
        "gliner": gliner,
    }


@app.post("/extract")
def extract(body: ExtractIn) -> dict:
    # Sync endpoint on purpose: FastAPI runs it in the threadpool, the event
    # loop stays free for /health, and ghost_b_local._INFER_LOCK serializes
    # concurrent extraction requests onto the single Metal device.
    if not body.tasks:
        return {"results": [], "metrics": _metrics([])}
    task_dicts = [t.model_dump() for t in body.tasks]
    try:
        raw = _extract_raw(task_dicts, body.enable_facts, body.schema_lens_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("extract failed (%d tasks)", len(task_dicts))
        raise HTTPException(status_code=500, detail=f"extraction failed: {exc}") from exc
    from services.ghost_b_local import LAST_TIMINGS
    return {"results": raw, "metrics": _metrics(raw), "timings": dict(LAST_TIMINGS)}

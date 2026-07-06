"""chunk_svc — remote chunk-stage sidecar ("chunk car").

Same elastic-fleet pattern as ghost_b_extract_svc, for the CHUNK stage: the
Mac backend was the chunking bottleneck (10 cores, 842% CPU under load,
2026-07-06) while the RTX box's desktop CPU idled. Run this on the box; the
backend probes CHUNK_REMOTE_URLS per doc and falls back to its local process
pool on any failure — a chunk car that joins/leaves like extraction cars.

Wire format: pickled (parse_result, config) inside a JSON envelope. Both ends
run the same repo checkout (PYTHONPATH -> <repo>/backend), so pickle is safe
and exact; a schema_version field guards drift. Trusted-LAN service — do not
expose beyond the LAN.

Run (Windows, from the repo root):
  $env:PYTHONPATH="E:\\polymath_v3.3\\backend"
  python -m uvicorn main:app --host 0.0.0.0 --port 8090
(working dir: scripts/apple_ml_services/chunk_svc)

Requires only tier_chunker's dependency slice (pydantic, fastapi, uvicorn,
plus the chunker's own imports) — no torch, no GPU.
"""

from __future__ import annotations

import base64
import logging
import os
import pickle
import time

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("chunk_svc")
logging.basicConfig(level=logging.INFO)

SCHEMA_VERSION = "polymath.chunk_rpc.v1"

app = FastAPI(title="polymath chunk_svc")

_ACTIVE = 0
_STARTED = time.time()


class ChunkIn(BaseModel):
    schema_version: str = Field(default=SCHEMA_VERSION)
    doc_id: str = ""
    corpus_id: str = ""
    # base64(pickle(parse_result)) and base64(pickle(IngestionConfig))
    parse_result_b64: str
    config_b64: str


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "chunk_svc",
        "schema_version": SCHEMA_VERSION,
        "active_requests": _ACTIVE,
        "uptime_s": round(time.time() - _STARTED, 1),
        "pid": os.getpid(),
    }


@app.post("/chunk")
def chunk(body: ChunkIn) -> dict:
    global _ACTIVE
    if body.schema_version != SCHEMA_VERSION:
        raise HTTPException(
            status_code=409,
            detail=f"schema mismatch: client={body.schema_version} "
            f"service={SCHEMA_VERSION} — update the repo checkout",
        )
    _ACTIVE += 1
    t0 = time.time()
    try:
        parse_result = pickle.loads(base64.b64decode(body.parse_result_b64))
        config = pickle.loads(base64.b64decode(body.config_b64))
        from services.ingestion import tier_chunker

        parents, children, injected_headers = tier_chunker.chunk(
            parse_result=parse_result,
            doc_id=body.doc_id,
            corpus_id=body.corpus_id,
            config=config,
        )
        payload = base64.b64encode(
            pickle.dumps((parents, children, injected_headers))
        ).decode()
        logger.info(
            "chunked doc=%s parents=%d children=%d in %.1fs",
            body.doc_id[:12],
            len(parents),
            len(children),
            time.time() - t0,
        )
        return {"schema_version": SCHEMA_VERSION, "result_b64": payload}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — client falls back to local
        logger.exception("chunk failed doc=%s", body.doc_id[:12])
        raise HTTPException(status_code=500, detail=f"chunk failed: {exc}") from exc
    finally:
        _ACTIVE -= 1

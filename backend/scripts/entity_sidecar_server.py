"""Entity-encoder host-MPS sidecar (release entity-sidecar-mps-v1).

Serves the EXISTING qualified entity providers (GLiNER2 baseline and
gliner-bi candidate) on host Metal, behind the same HTTP pattern as the
Relex sidecar. No provider logic is reimplemented: this wraps
services.extraction provider code verbatim with a device override, so
decisions are byte-identical to the placement A/B qualification.
Composite folding stays worker-side (structural reconciliation is
pipeline knowledge, not transport).

Run under the MAIN interpreter (gliner2 + gliner 0.2.26 + torch MPS):

    local_ghost_b/.venv/bin/python backend/scripts/entity_sidecar_server.py

Contract entity-predict-v1 — POST /predict
  request:  {"provider": "gliner2"|"gliner_bi", "texts": [...],
             "adapters"?: [...], "threshold"?: float, "batch_size"?: int}
  response: {"contract": "entity-predict-v1", "release": ...,
             "results": [[{text,start,end,entity_type,confidence,facet}]]}
GET /health → release + device proof.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_BACKEND = str(Path(__file__).resolve().parents[1])
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("GLINER_BI_DEVICE", "mps")

HOST = "127.0.0.1"
PORT = int(os.environ.get("ENTITY_SIDECAR_PORT", "8738"))
RELEASE = "entity-sidecar-mps-v1"
CONTRACT = "entity-predict-v1"

_LOCK = threading.Lock()
_STATE: dict = {}


def _load():
    import torch

    if not torch.backends.mps.is_available():
        if os.environ.get("ENTITY_SIDECAR_ALLOW_CPU") != "1":
            print("FATAL: MPS unavailable and ENTITY_SIDECAR_ALLOW_CPU!=1",
                  file=sys.stderr)
            raise SystemExit(2)

    from ab_gliner2_device import DeviceProvider  # scripts dir, proven A/B wrapper
    from services.extraction.entity_encoder import GLiNERBiProvider

    started = time.perf_counter()
    gliner2 = DeviceProvider("mps")
    gliner2._model()  # warm
    bi = GLiNERBiProvider()
    bi._model()  # warm (GLINER_BI_DEVICE=mps)
    _STATE.update(
        gliner2=gliner2, gliner_bi=bi,
        load_seconds=round(time.perf_counter() - started, 1), device="mps",
    )
    print(f"entity sidecar warm on mps ({_STATE['load_seconds']}s) — {HOST}:{PORT}")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        if self.path != "/health":
            return self._send(404, {"error": "unknown path"})
        self._send(200, {
            "release": RELEASE, "contract": CONTRACT,
            "device": _STATE.get("device"),
            "providers": {
                "gliner2": getattr(_STATE.get("gliner2"), "release", "gliner2-device"),
                "gliner_bi": getattr(_STATE.get("gliner_bi"), "release", ""),
            },
            "load_seconds": _STATE.get("load_seconds"),
        })

    def do_POST(self):  # noqa: N802
        if self.path != "/predict":
            return self._send(404, {"error": "unknown path"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length))
            provider = _STATE.get(str(payload.get("provider") or ""))
            texts = payload.get("texts")
            if provider is None or not isinstance(texts, list):
                return self._send(400, {"error": "provider must be gliner2|gliner_bi; texts must be a list"})
            started = time.perf_counter()
            with _LOCK:
                rows = provider.predict_entities(
                    texts,
                    batch_size=int(payload.get("batch_size") or 4),
                    threshold=float(payload.get("threshold") or 0.5),
                    adapters=tuple(payload.get("adapters") or ()),
                )
            results = [
                [
                    {"text": p.text, "start": p.start, "end": p.end,
                     "entity_type": p.entity_type,
                     "confidence": p.confidence, "facet": p.facet}
                    for p in row
                ]
                for row in rows
            ]
            self._send(200, {
                "contract": CONTRACT, "release": RELEASE,
                "device": _STATE["device"], "results": results,
                "seconds": round(time.perf_counter() - started, 3),
            })
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt, *args):
        pass


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    _load()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

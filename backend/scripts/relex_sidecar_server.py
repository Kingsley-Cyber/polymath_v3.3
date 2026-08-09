"""GLiNER-Relex host-MPS sidecar (release relex-large-mps-sidecar-v1).

Runs OUTSIDE Docker on the macOS host — Docker Desktop has no MPS
passthrough (measured GLiNER2 container tax: 14.6x vs host Metal), so the
neural stage lives here and workers call over HTTP:

    host:       127.0.0.1:8737
    container:  host.docker.internal:8737

Run under the relex interpreter (NOT the main venv — gliner>=0.2.28 needed):

    .venv-relex/bin/python backend/scripts/relex_sidecar_server.py

Contract relex-infer-v1 — POST /infer
  request:  {"texts": [...], "entity_labels"?: [...], "relation_labels"?: [...],
             "entity_threshold"?: float, "relation_threshold"?: float}
  response: {"contract": "relex-infer-v1", "release": ..., "results": [
              {"entities": [{start,end,text,label,score}],
               "relations": [{"head": {start,end,text}, "tail": {...},
                              "label": str, "score": float}]} per text],
             "seconds": float}
GET /health → release manifest + device proof.

GPU is REQUIRED (owner directive): if MPS is unavailable the server exits
unless RELEX_SIDECAR_ALLOW_CPU=1 is set explicitly.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

_CONFIG_PATH = os.environ.get("RELEX_SIDECAR_CONFIG") or str(
    Path(__file__).resolve().parents[2] / "config" / "relex_sidecar.yaml"
)
CONFIG = yaml.safe_load(Path(_CONFIG_PATH).read_text())
HOST = CONFIG["runtime"]["host"]
PORT = int(os.environ.get("RELEX_SIDECAR_PORT", CONFIG["runtime"]["port"]))

_LOCK = threading.Lock()
_STATE: dict = {}


def _load_model():
    import torch
    from gliner import GLiNER

    device = CONFIG["runtime"]["device"]
    if device == "cuda" and not torch.cuda.is_available():
        if os.environ.get("RELEX_SIDECAR_ALLOW_CPU") != "1":
            print("FATAL: release pins device: cuda but CUDA is unavailable — "
                  "refusing silent CPU fallback", file=sys.stderr)
            raise SystemExit(2)
        device = "cpu"
    if device == "mps" and not torch.backends.mps.is_available():
        if os.environ.get("RELEX_SIDECAR_ALLOW_CPU") != "1":
            print("FATAL: MPS unavailable and RELEX_SIDECAR_ALLOW_CPU!=1 — "
                  "refusing silent CPU fallback (release pins device: mps)",
                  file=sys.stderr)
            raise SystemExit(2)
        device = "cpu"
    started = time.perf_counter()
    model = GLiNER.from_pretrained(
        CONFIG["model"]["id"], revision=CONFIG["model"]["revision"],
    )
    model.eval()
    if device != "cpu":
        model = model.to(device)
    _STATE.update(model=model, device=device,
                  load_seconds=round(time.perf_counter() - started, 1),
                  torch=torch)
    print(f"relex sidecar warm: {CONFIG['model']['id']} on {device} "
          f"({_STATE['load_seconds']}s) — {HOST}:{PORT}")


def _infer(payload: dict) -> dict:
    texts = payload["texts"]
    entity_labels = payload.get("entity_labels") or CONFIG["labels"]["entities"]
    relation_labels = payload.get("relation_labels") or CONFIG["labels"]["relations"]
    ent_thr = float(payload.get("entity_threshold") or CONFIG["thresholds"]["entity"])
    rel_thr = float(payload.get("relation_threshold") or CONFIG["thresholds"]["relation"])
    torch = _STATE["torch"]
    results = []
    started = time.perf_counter()
    with _LOCK, torch.no_grad():
        for text in texts:
            entities, relations = _STATE["model"].inference(
                texts=[text], labels=list(entity_labels),
                relations=list(relation_labels), threshold=ent_thr,
                relation_threshold=rel_thr, return_relations=True,
                flat_ner=False,
            )
            ents = [
                {"start": int(e["start"]), "end": int(e["end"]),
                 "text": str(e["text"]), "label": str(e.get("label") or ""),
                 "score": float(e.get("score") or 0.0)}
                for e in (entities[0] if entities else [])
            ]
            rels = [
                {"head": {"start": int(r["head"]["start"]), "end": int(r["head"]["end"]),
                          "text": str(r["head"]["text"])},
                 "tail": {"start": int(r["tail"]["start"]), "end": int(r["tail"]["end"]),
                          "text": str(r["tail"]["text"])},
                 "label": str(r.get("relation") or ""),
                 "score": float(r.get("score") or 0.0)}
                for r in (relations[0] if relations else [])
                if isinstance(r.get("head"), dict) and isinstance(r.get("tail"), dict)
            ]
            results.append({"entities": ents, "relations": rels})
    return {"contract": CONFIG["output_contract"], "release": CONFIG["release"],
            "device": _STATE["device"], "results": results,
            "seconds": round(time.perf_counter() - started, 3)}


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
            "release": CONFIG["release"], "contract": CONFIG["output_contract"],
            "model": CONFIG["model"], "device": _STATE.get("device"),
            "load_seconds": _STATE.get("load_seconds"),
            "thresholds": CONFIG["thresholds"],
        })

    def do_POST(self):  # noqa: N802
        if self.path != "/infer":
            return self._send(404, {"error": "unknown path"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload.get("texts"), list):
                return self._send(400, {"error": "texts must be a list"})
            self._send(200, _infer(payload))
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

    def log_message(self, fmt, *args):  # quiet access log
        pass


def main() -> int:
    _load_model()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

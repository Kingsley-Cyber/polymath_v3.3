# HANDOFF PROMPT — paste everything below this line into Claude on the RTX workstation

---

You are setting up a **GLiNER-Relex CUDA inference sidecar** on this machine (an RTX 6000 Pro 96GB workstation). It will serve entity/relation extraction over HTTP to a Mac Studio on the same LAN, which runs the Polymath knowledge-graph pipeline. The Mac's worker enforces a release-pin handshake on every call, so this deployment must match the frozen release identity **exactly**.

## Hard constraints — violating any of these makes the deployment useless

1. **Exact model pin, no substitutions**: `knowledgator/gliner-relex-large-v1.0`, revision `4aedc9226a5ac9e2f6b5ea3e91c1ee577c88a290`. Do not "upgrade", do not use the base variant, do not use an MLX/ONNX/quantized conversion.
2. **Weights must sha256-verify** to `7c5bd751e1b24e4254d70fe4355a986cd65400676ce3735f7752429fcc26960a` before serving. If the hash mismatches, STOP and report — do not serve.
3. **Pinned inference deps**: `gliner==0.2.28`, `transformers==5.13.1`, torch ≥2.13 with CUDA support for Blackwell (sm_120 → use the **cu128** wheel index; fall back to cu126 only if cu128 wheels are unavailable, and verify a test tensor op runs on GPU either way).
4. **Never enable CPU fallback.** The server exits by design if CUDA is unavailable. Do not set `RELEX_SIDECAR_ALLOW_CPU`.
5. **Do not edit** the thresholds, labels, or contract in the config. They are release properties.
6. **LAN-only exposure**: bind 0.0.0.0 but only open firewall port 8737 to the local network. Do not tunnel or expose publicly.

## Step 1 — environment

- If this is Linux: proceed directly.
- If this is Windows: use **WSL2** (Ubuntu) with NVIDIA CUDA passthrough (`nvidia-smi` must work inside WSL). Note for later: WSL2 is NAT'd — Step 6 includes the port-forward so the Mac can reach it.

```bash
mkdir -p ~/relex-sidecar/config ~/relex-sidecar/scripts && cd ~/relex-sidecar
python3 -m venv .venv && source .venv/bin/activate
pip install --upgrade pip
pip install torch --index-url https://download.pytorch.org/whl/cu128 \
  || pip install torch --index-url https://download.pytorch.org/whl/cu126
pip install "gliner==0.2.28" "transformers==5.13.1" pyyaml huggingface_hub
python -c "import torch; assert torch.cuda.is_available(), 'CUDA NOT AVAILABLE'; \
x = torch.randn(64,64,device='cuda') @ torch.randn(64,64,device='cuda'); \
print('CUDA OK:', torch.cuda.get_device_name(0))"
```

If the CUDA assert fails, stop and report the driver/torch situation instead of working around it.

## Step 2 — write the config

Create `config/relex_sidecar_cuda.yaml` with EXACTLY:

```yaml
release: relex-large-cuda-sidecar-v1
role: qualification_candidate
model:
  id: knowledgator/gliner-relex-large-v1.0
  revision: 4aedc9226a5ac9e2f6b5ea3e91c1ee577c88a290
  weights_sha256: 7c5bd751e1b24e4254d70fe4355a986cd65400676ce3735f7752429fcc26960a
runtime:
  device: cuda
  interpreter: .venv
  host: 0.0.0.0
  port: 8737
thresholds:
  entity: 0.30
  relation: 0.30
labels:
  entities: [person, organization, location, product, software, model,
             document, dataset, concept, method, process, event, metric,
             system, component, other]
  relations: ["is a", "part of", "owns", "member of", "created by",
              "located in", "uses", "depends on", "implements", "supports",
              "produces", "consumes", "derived from", "defines", "measures",
              "causes", "enables", "precedes"]
output_contract: relex-infer-v1
```

## Step 3 — write the server

Create `scripts/relex_sidecar_server.py` with EXACTLY the following content (do not modify it):

```python
"""GLiNER-Relex sidecar. GET /health, POST /infer (contract relex-infer-v1).
GPU required: exits rather than silently falling back to CPU."""
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
```

Note: the script resolves its default config two directories up; you will pass `RELEX_SIDECAR_CONFIG` explicitly, so the layout of `~/relex-sidecar` is fine as created.

## Step 4 — download and VERIFY the weights

```bash
cd ~/relex-sidecar && source .venv/bin/activate
python - << 'EOF'
import hashlib
from pathlib import Path
from huggingface_hub import hf_hub_download
path = hf_hub_download("knowledgator/gliner-relex-large-v1.0", "pytorch_model.bin",
                       revision="4aedc9226a5ac9e2f6b5ea3e91c1ee577c88a290")
digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
expected = "7c5bd751e1b24e4254d70fe4355a986cd65400676ce3735f7752429fcc26960a"
assert digest == expected, f"WEIGHTS HASH MISMATCH: {digest}"
print("weights verified:", digest[:16])
EOF
```

If the file `pytorch_model.bin` doesn't exist at that revision, list the repo files at that revision, hash whichever primary weights file exists, and REPORT the filename+hash rather than guessing — do not serve unverified weights.

## Step 5 — launch and smoke-test

```bash
cd ~/relex-sidecar && source .venv/bin/activate
RELEX_SIDECAR_CONFIG=config/relex_sidecar_cuda.yaml python scripts/relex_sidecar_server.py &
sleep 60   # model load
curl -s http://127.0.0.1:8737/health | python3 -m json.tool
curl -s -X POST http://127.0.0.1:8737/infer -H 'Content-Type: application/json' \
  -d '{"texts": ["Harbor Gateway uses Envoy as its edge proxy and depends on Identity Service."]}' \
  | python3 -m json.tool
```

Health must show `"device": "cuda"` and `"release": "relex-large-cuda-sidecar-v1"`. The infer call must return entities (expect spans like "Harbor Gateway", "Envoy") and relations, with a `seconds` value — record it.

## Step 6 — make it reachable from the LAN

- Linux: `sudo ufw allow from 192.168.0.0/16 to any port 8737` (adjust subnet to the actual LAN).
- Windows/WSL2 (both needed):
  1. Windows firewall inbound rule for TCP 8737 (private networks only).
  2. Either enable WSL2 **mirrored networking** (`.wslconfig`: `networkingMode=mirrored`, then `wsl --shutdown` and relaunch), or NAT port-proxy:
     `netsh interface portproxy add v4tov4 listenport=8737 listenaddress=0.0.0.0 connectport=8737 connectaddress=$(wsl hostname -I | awk '{print $1}')` (run in elevated PowerShell; note the WSL IP changes on reboot — mirrored mode is the durable choice).
- Verify from ANOTHER machine on the LAN if possible: `curl http://<this-box-lan-ip>:8737/health`.

## Step 7 — supervision (survive reboots/crashes)

Linux (or WSL2 with systemd enabled): create `/etc/systemd/system/relex-sidecar.service`:

```ini
[Unit]
Description=GLiNER-Relex CUDA sidecar (pinned release)
After=network.target

[Service]
User=%i-replace-with-your-username
WorkingDirectory=/home/YOURUSER/relex-sidecar
Environment=RELEX_SIDECAR_CONFIG=config/relex_sidecar_cuda.yaml
ExecStart=/home/YOURUSER/relex-sidecar/.venv/bin/python scripts/relex_sidecar_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then `sudo systemctl daemon-reload && sudo systemctl enable --now relex-sidecar` and kill the Step-5 foreground process. Verify health again after `sudo systemctl restart relex-sidecar` and after a `kill -9` of the python PID (it must respawn).

## Step 8 — completion report (give this back to the user verbatim)

Report exactly:
1. The machine's **LAN IP** and the health JSON from `curl http://<lan-ip>:8737/health`.
2. torch version + CUDA wheel used + `nvidia-smi` one-liner (driver, GPU name).
3. The weights sha16 you verified.
4. The `seconds` value from the smoke inference.
5. Supervision status (`systemctl is-active relex-sidecar` or what you set up instead).
6. Anything you had to deviate on, explicitly flagged.

The user will then hand the LAN IP to the Mac-side agent, which routes the
pipeline to this box in qualification mode and runs the acceptance battery
against it. **Do not** attempt to connect to the Mac, its databases, or
anything beyond serving this HTTP endpoint.

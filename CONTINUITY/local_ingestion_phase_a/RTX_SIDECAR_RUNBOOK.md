# RTX extraction sidecar — setup runbook

Goal: run the SAME `ghost_b_extract_svc` sidecar on the CUDA box (the one that
fine-tuned glirel_ghost_b_v1), and point the Mac's worker at it over the LAN.
Nothing else moves — the Mac keeps chunking, embedding, Qdrant/Neo4j/Mongo,
and the app. Expected: extraction stops being the bottleneck; the 338 MB
backfill lands well under a day (the Mac's measured ceiling is ~3 days — its
unified-memory bandwidth binds DeBERTa-large; the RTX has dedicated VRAM
bandwidth + tensor cores + full CUDA op coverage).

## On the RTX box (Linux; Windows notes at the bottom)

### 1. Clone
```bash
git clone https://github.com/Kingsley-Cyber/polymath_v3.3.git
cd polymath_v3.3
```

### 2. Python 3.11 venv
glirel uses PEP 604 unions — needs Python ≥3.10; 3.11 is the validated version.
```bash
python3.11 -m venv .venv_sidecar
source .venv_sidecar/bin/activate
```

### 3. Install — THE PINS ARE LOAD-BEARING
glirel 1.2.1 breaks against huggingface_hub ≥1.0 (`_from_pretrained() missing
proxies/resume_download`), and it doesn't declare loguru. This exact set is the
working combination validated on the Mac:
```bash
pip install torch --index-url https://download.pytorch.org/whl/cu124   # match your CUDA (cu121/cu124/...)
pip install "transformers>=4.48,<5.0" "huggingface_hub<1.0" \
            gliner==0.2.26 glirel==1.2.1 loguru \
            pydantic fastapi "uvicorn[standard]"
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

### 4. Weights (1.7 GB — NOT in git)
The loader looks at `models/glirel_ghost_b_v1/best/` (needs `pytorch_model.bin`
+ `glirel_config.json` + `labels.json`). Two options:
```bash
# A) you trained v1 on this box — point at the training output directly:
export GLIREL_CKPT_DIR=/path/to/your/glirel_ghost_b_v1/best

# B) copy from the Mac:
mkdir -p models
scp -r king@<mac-ip>:/Users/king/polymath_v3.3/models/glirel_ghost_b_v1 models/
```
GLiNER (`urchade/gliner_medium-v2.1`, ~500 MB) auto-downloads from HF on first
start — the box needs internet once, or pre-seed the HF cache.

### 5. Launch
```bash
cd scripts/apple_ml_services
GHOST_B_GLINER_BATCH=128 GHOST_B_GLIREL_BATCH=256 GHOST_B_FACET_BATCH=128 \
  ../../.venv_sidecar/bin/python -m uvicorn \
  ghost_b_extract_svc.main:app --host 0.0.0.0 --port 8084
```
(Prefix `GLIREL_CKPT_DIR=...` if using option A. Batches are 2× the Mac's —
dedicated VRAM takes it easily; raise further if `nvidia-smi` shows headroom.)
Watch the log for `warmup complete — models resident`.

### 6. Firewall — LAN only, never tunnel/port-forward this
The sidecar is an unauthenticated internal service.
```bash
sudo ufw allow from <mac-ip> to any port 8084 proto tcp
```

## On the Mac

### 7. Verify reachability, then point the worker at the RTX
```bash
curl http://<rtx-ip>:8084/health
# expect: {"status":"ok", ..., "warm":true, "device":"cuda (NVIDIA RTX ...)"}
```
Add one line to `docker-compose.override.yml` under `backend: environment:`:
```yaml
      LOCAL_GHOST_B_EXTRACT_URL: http://<rtx-ip>:8084
```
then `docker compose up -d backend`. The Mac's own :8084 sidecar can be stopped
(`pkill -f ghost_b_extract_svc`) or left running — the URL decides who works.

### 8. Smoke before the backfill
Re-ingest one pilot file into a scratch corpus; confirm `phase=verify ok=true`
and the RTX log shows the `ghost_b_local: N chunks in Xs` stage line with a
per-chunk time that should embarrass the Mac's 420 ms.

### 9. Revert / fallback
Remove the env line + `docker compose up -d backend` → back to the Mac sidecar.
If the RTX is unreachable mid-run, ingestion FAILS LOUDLY per doc (by design,
no silent fallback); resume the batch after restoring either sidecar.

## Windows box variant
- venv activate: `.venv_sidecar\Scripts\activate`; same pip installs.
- Firewall: `New-NetFirewallRule -DisplayName ghostb -Direction Inbound
  -LocalPort 8084 -Protocol TCP -RemoteAddress <mac-ip> -Action Allow`
- Paths in `GLIREL_CKPT_DIR` use Windows form; everything else identical.
- WSL2 also works (treat as Linux; mind WSL's NAT when exposing 8084).

## Notes
- Scores on CUDA can differ from MPS in float tails (same caveat as any
  cross-device move); the backfill running entirely on one device = one
  consistent basis.
- `GHOST_B_GLIREL_CPU_LANE` stays off (pointless next to a CUDA card).
- The embed-reconcile worker fix (PILOT_REPORT "Resilience #2") is required
  before the unattended backfill regardless of where extraction runs.

# Local extraction — fresh-machine setup

Polymath's entity/relation/fact extraction (the "Ghost B" lane) runs fully
locally on machines YOU configure — any mix of an Apple Silicon Mac, a CUDA
GPU box, or just the machine running Docker. Endpoints are managed in the app
(Settings → Ingestion → Extraction Engines): add a row per machine, toggle it
on/off, hit **Validate** to get a per-engine deploy-readiness checklist probed
from the backend's own network position.

## 0. The stack itself

```
cp .env.example .env        # fill in passwords/keys
docker compose up -d        # backend, frontend, Mongo, Neo4j, Qdrant, ...
```

`LOCAL_GHOST_B_EXTRACT_URL` in the env is only the SEED for the engine list —
after first boot, configuration lives in Settings and applies on the next
ingest without restarts. `POLYMATH_INGEST_SOURCE_ROOT` mounts the host folder
that local-folder ingestion reads (appears as `/ingest-source/...` in the app).

## 1. Models (per extraction machine)

One command, idempotent, selective (no multi-GB surprise snapshots):

```
pip install huggingface_hub
python scripts/bootstrap_models.py --gliner torch          # Mac / CPU / plain CUDA
python scripts/bootstrap_models.py --gliner onnx           # CUDA box, ONNX lane (fastest)
python scripts/bootstrap_models.py --gliner both --glirel-zero-shot
```

It prints the exact env wiring for the lane you chose. The fine-tuned GLiREL
relation model is preferred when you have its checkpoint (`GLIREL_CKPT_DIR`);
without it the zero-shot fallback model is used.

## 2. The sidecar process (per extraction machine)

The extraction service is `ghost_b_extract_svc` (FastAPI, default port 8084):

- **macOS**: `scripts/apple_ml_services/start.sh` with
  `START_GHOST_B_EXTRACT=true`, or a LaunchAgent running
  `local_ghost_b/.venv/bin/python -m uvicorn ghost_b_extract_svc.main:app
  --host 0.0.0.0 --port 8084` from `scripts/apple_ml_services/`.
- **Windows CUDA box**: `scripts/apple_ml_services/run_sidecar_windows.ps1`
  (torch lane) or `run_sidecar_onnx.ps1` (ONNX lane). Boot persistence via the
  Scheduled Task XMLs in `scripts/apple_ml_services/windows_tasks/` (README
  there covers import + the one `<UserId>` edit a new box needs).
- **ONNX on Blackwell/cu130 torch**: PyPI `onnxruntime-gpu` is CUDA-12-only
  and will NOT work alongside torch cu130 — use the official Microsoft
  CUDA-13 ORT nightly. Details + verified versions:
  `CONTINUITY/local_ingestion_phase_a/SESSION_STATE_2026-06-11.md`.

`GET :8084/health` self-describes the lane: backend (torch/onnx), device, and
— for ONNX — the ACTIVE execution providers, so a GPU claim that silently fell
back to CPU is visible remotely.

## 3. Wire it up in the app

Settings → Ingestion → Extraction Engines:

1. Add a row per machine: label + `http://<lan-ip>:8084` (use
   `http://host.docker.internal:<port>` for a sidecar on the same machine as
   Docker). Toggle ON the engines you want; order = preference.
2. **Save**, then **Validate** — every row gets checkmarks (reachable,
   healthy, warm, model loaded, GPU active, version match) and the card shows
   a deploy-ready verdict. Validation runs from the backend container, i.e.
   exactly the network position the ingestion worker uses.
3. Ingest. The worker re-reads this config per run, health-probes enabled
   engines per document, and fans slices out across the live ones. A
   powered-off box is skipped automatically.

Keep slow engines DISABLED rather than enabled-as-backup: slices round-robin
evenly across all live enabled engines, so one slow engine drags the batch.
Flip the toggle when you actually want the fallback.

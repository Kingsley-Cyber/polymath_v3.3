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
# Full local stack — stock GLiNER + the custom fine-tuned Ghost B GLiREL:
python scripts/bootstrap_models.py --gliner torch --glirel-custom   # Mac / CPU / plain CUDA
python scripts/bootstrap_models.py --gliner onnx  --glirel-custom   # CUDA box, ONNX lane (fastest)
```

Two models back the extraction lane, distributed two different ways:

- **GLiNER (entity pass)** — stock `urchade/gliner_medium-v2.1`, pulled from HF
  Hub. Not custom; the customization is the zero-shot *labels* in
  `pipeline_config`, which ship in the repo as code.
- **GLiREL (relation pass)** — the **custom fine-tuned Ghost B model** (~1.7 GB,
  30 predicates). `--glirel-custom` downloads it from HF Hub into
  `models/glirel_ghost_b_v1/best/`, where the sidecar loads it by default. This
  is what makes the local graph *typed*; without it, relations fall back to
  zero-shot GLiREL (`--glirel-zero-shot`, weaker). Weights are NOT in git (too
  large); HF Hub is the channel, same as GLiNER.

You can also skip the local copy and point `GLIREL_CKPT_DIR` at the HF repo id
directly — `from_pretrained` downloads + caches it.

### Maintainer: publishing the custom GLiREL (one time)

The `--glirel-custom` download only works once the checkpoint is on HF Hub.
To publish it from a machine that has the trained weights:

```
export HF_TOKEN=hf_xxx        # a WRITE token from hf.co/settings/tokens
python scripts/publish_glirel_to_hf.py            # -> Sambenja1/glirel-ghost-b-v1 (public)
# or:  python scripts/publish_glirel_to_hf.py --repo you/your-glirel --private
```

If you publish to a non-default repo, users pass `--glirel-repo you/your-glirel`
(or set `GHOST_B_GLIREL_HF_REPO`).

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

The backend extraction client rejects ONNX sidecars whose active providers do
not include `CUDAExecutionProvider` unless you explicitly set:

```
LOCAL_GHOST_B_ALLOW_ONNX_CPU_FALLBACK=true
```

Leave that false for production. CPU fallback is useful for tiny smoke tests,
but it can make real ingestions look stuck while Mongo/Qdrant/Neo4j are waiting
for extraction slices.

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

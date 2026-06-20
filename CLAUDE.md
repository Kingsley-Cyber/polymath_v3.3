# Polymath v3.3 — agent & contributor guide

Read this before deploying or running the stack. It exists to stop a specific,
costly mistake from recurring on any device or by any AI agent.

## 🚨 Deploying / recreating the backend — DO NOT get this wrong

The backend reaches its embedder + reranker via env vars. `docker-compose.yml`
HARDCODES the **compose defaults** `EMBEDDER_URL=http://embedder:80` and
`RERANKER_URL=http://reranker:8080`, which point at profile-gated containers
(`local-embed` / `local-rerank`) that **are not running on most devices** (the
reranker image is CUDA-only). The real endpoints come from an **override file**:

- **Apple/Mac:** `docker-compose.apple-mlx.yml` (and the auto-merged
  `docker-compose.override.yml`) → host MLX sidecars
  `EMBEDDER_URL=http://host.docker.internal:8082`,
  `RERANKER_URL=http://host.docker.internal:8081`.
- **Linux/NVIDIA:** the `local-embed` / `local-rerank` compose profiles.

**The trap:** Docker Compose auto-merges `docker-compose.override.yml` ONLY when
you pass NO `-f`. Running `docker compose -f docker-compose.yml ... up backend`
(base file alone) **silently drops the override** → the backend boots with the
dead `embedder:80` / `reranker:8080` defaults → `embed_query` ConnectErrors with
no fallback → **vector / hybrid / graph retrieval return 0–degraded results while
the container still reports `healthy`** (the healthcheck is liveness-only). It is
SILENT. This already caused an outage (2026-06-20).

### ✅ The only correct ways to (re)deploy the backend

```bash
# Mac — canonical (matches scripts/setup_apple_mlx.sh):
docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml up -d --build backend
# …or the full one-shot setup:
bash scripts/setup_apple_mlx.sh

# Any device — bare form also works (auto-merges docker-compose.override.yml),
# but ONLY when run from the repo root with NO -f:
cd <repo> && docker compose up -d --build backend
```

### ❌ NEVER do this
```bash
docker compose -f docker-compose.yml up -d --build backend   # drops the override → dead embedder
```

### ✅ ALWAYS verify after any backend (re)deploy
```bash
bash scripts/verify_backend_runtime.sh
```
It fails LOUD (non-zero exit) if the backend's resolved `EMBEDDER_URL` /
`RERANKER_URL` are the dead defaults or if a live embed returns no vector — i.e.
it catches exactly the silent misw iring above. The backend ALSO runs an
embedder self-check at startup and logs `CRITICAL` with the fix if embedding
fails (see `backend/main.py` lifespan), so a bad deploy can never be silent.

Quick manual check: `docker exec polymath_v33-backend-1 sh -c 'echo $EMBEDDER_URL'`
must show `host.docker.internal:8082` (Mac), never `embedder:80`.

## Code & deploy mechanics
- Backend code is **baked into the image at build time** (COPY, not a volume
  mount). A code change requires `--build` to take effect; an env-only fix just
  needs a recreate. `tests/` is NOT baked into the image.
- The MLX embedder (`:8082`) and Qwen3 reranker (`:8081`) are **host sidecars**
  managed by `scripts/setup_apple_mlx.sh` / verified by `scripts/smoke_apple_mlx.sh`.

## Tests
- **Portable invariants** (`backend/tests/test_retrieval_quality_invariants.py`,
  `test_retrieval_ranking_policy.py`): pure logic, NO live stack. Run anywhere:
  `cd backend && pytest tests/test_retrieval_quality_invariants.py -q`. CI runs
  these on every push (`.github/workflows/retrieval-quality.yml`).
- **Live e2e** (`backend/tests/test_*_e2e.py`): import-safe scripts that need the
  running stack + seeded data. Run via `docker cp test.py polymath_v33-backend-1:/app/_x.py
  && docker exec -w /app polymath_v33-backend-1 python _x.py`. They are NOT pytest
  tests (no module-level execution at collection).
- **Prove before commit:** non-trivial backend changes get an automated asserting
  test (non-zero exit on fail) run GREEN before committing — log-greps are not proof.

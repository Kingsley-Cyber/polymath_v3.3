# Polymath v3.3 — agent & contributor guide

Read this before deploying or running the stack. It exists to stop a specific,
costly mistake from recurring on any device or by any AI agent.

## 🚨 Deploying / recreating the backend — DO NOT get this wrong

The backend reaches its embedder + reranker via env vars. The correct values are
platform-specific:

- **Apple/Mac:** use `docker-compose.apple-mlx.yml` → host MLX sidecars
  `EMBEDDER_URL=http://host.docker.internal:8082`,
  `RERANKER_URL=http://host.docker.internal:8081`.
- **Linux/NVIDIA / Windows RTX:** use the default `docker-compose.yml` plus the
  `local-embed` / `local-rerank` profiles from `.env`, where
  `EMBEDDER_URL=http://embedder:80` and `RERANKER_URL=http://reranker:8080` are
  correct internal Docker network URLs.

**The trap:** `docker-compose.override.yml` is auto-merged by Docker Compose and
must stay local-only. It is gitignored. Do not commit machine-specific sidecar
IPs or host paths. Use `docker-compose.override.example.yml` as the template if
you need local experiments.

### ✅ The only correct ways to (re)deploy the backend

```bash
# Mac — canonical (matches scripts/setup_apple_mlx.sh):
docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml up -d --build backend
# …or the full one-shot setup:
bash scripts/setup_apple_mlx.sh

# RTX/NVIDIA — canonical after bootstrap:
cd <repo> && docker compose up -d --build backend
```

### ❌ NEVER do this
```bash
git add docker-compose.override.yml   # commits private machine routing
```

### ✅ ALWAYS verify after any backend (re)deploy
```bash
bash scripts/verify_backend_runtime.sh
```
It fails LOUD (non-zero exit) if a live embed through the backend returns no
vector. The backend ALSO runs an embedder self-check at startup and logs
`CRITICAL` with the fix if embedding fails (see `backend/main.py` lifespan), so a
bad deploy can never be silent.

Quick manual check: `docker exec polymath_v33-backend-1 sh -c 'echo $EMBEDDER_URL'`
must show `host.docker.internal:8082` on Apple MLX, or `embedder:80` on RTX.

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

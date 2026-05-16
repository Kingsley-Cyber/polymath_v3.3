# Agent prompt — Apple Silicon (MLX) Polymath setup

Paste this into Claude Code or a similar coding agent when bringing up
Polymath on a Darwin/arm64 host (M1 / M2 / M3 / M4, Pro / Max / Ultra).

This prompt assumes the agent is already inside a freshly cloned
`polymath_v3.3` working tree.

---

## The prompt

```
You are bringing up Polymath v3.3 on Apple Silicon (Darwin/arm64). Do
NOT use the NVIDIA workstation profile or CUDA-targeted compose
files — Docker Desktop on macOS cannot pass through the Apple GPU.

Architecture you'll use:
  • Core stack runs in Docker (Mongo, Qdrant, Neo4j, Redis, LiteLLM,
    backend, frontend, MCP).
  • Embedder, reranker, and docling run HOST-NATIVE under a LaunchAgent
    (com.polymath.apple-ml) and are reached over host.docker.internal.
  • Backend is retargeted at the host services via
    docker-compose.apple-mlx.yml.

Run these in order. After each step, confirm it succeeded before
moving to the next. If a step fails, stop and surface the error
verbatim — do not paper over it.

Step 1 — Generate secrets and bootstrap the runtime layout.
  bash scripts/bootstrap-runtime.sh --generate-secrets --stage-models

Step 2 — Install the host-native MLX sidecars.
  bash scripts/install_apple_mlx_runtime.sh
  This will:
    - reject non-Darwin / non-arm64 hosts
    - stage code to ~/PolymathRuntime/apple_ml_services/
    - install a uv venv with the pinned requirements
    - pre-pull mlx-community/Qwen3-Embedding-0.6B-mxfp8 and
      jinaai/jina-reranker-v3-mlx into ~/PolymathRuntime/volumes/hf-cache
    - install the LaunchAgent (auto-restart)
    - smoke /info, /health, /health on ports 8082, 8081, 8500

Step 3 — Bring up Docker with the Apple MLX override.
  docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml up -d --build

Step 4 — Run the end-to-end smoke.
  bash scripts/smoke_apple_mlx.sh
  The reranker readiness and ordering checks must pass. /info.ready must
  be true, /rerank must return results[{index,score,text}], and the two
  object-pattern documents must outrank the lemonade document. If any of
  that fails, stop before ingestion; the Apple reranker is not trustworthy.

Step 5 — Verify env wiring inside the backend container.
  docker exec polymath_v33-backend-1 sh -c '
    echo EMBEDDER_URL=$EMBEDDER_URL
    echo RERANKER_URL=$RERANKER_URL
    echo DOCLING_URL=$DOCLING_URL
    echo RERANKER_SCORE_SCALE=$RERANKER_SCORE_SCALE
  '
  All four must show host.docker.internal addresses + cosine. If any
  point at the in-cluster service names, the override didn't apply —
  re-run docker compose with both -f flags.

Step 6 — Health check the full stack.
  docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml ps
  All five core services (backend, mongodb, qdrant, neo4j, litellm)
  must be (healthy). Embedder/reranker/docling will NOT appear because
  they're host-native — that's correct.

Constraints you must respect:
  • Never change EMBEDDING_DIMENSION or the embedding model unless
    you also re-ingest every corpus — Qdrant collections are dimension-
    locked.
  • Never push to main; commit on a feature branch.
  • Treat the repo's Apple sidecars as canonical by default. The installer
    refreshes host copies on each run unless
    POLYMATH_APPLE_MLX_PROTECT_HOST_SIDECARS=1 is explicitly set.
  • The reranker uses Jina's official MLX implementation from
    jinaai/jina-reranker-v3-mlx. Do not replace it with a zero-score
    scaffold or a hand-written projector unless you prove the smoke test
    still passes.
  • The reranker MUST run with RERANKER_SCORE_SCALE=cosine. The override
    sets it; do not strip it.
  • Do not use the DeepSeek API directly — only via LiteLLM.
  • Do not commit anything to .env (it's gitignored and contains
    secrets).

What "done" looks like:
  • smoke_apple_mlx.sh passes including the rerank ordering assertion
  • curl http://localhost:8081/info shows ready=true and score_scale=cosine
  • backend container reports (healthy)
  • a sample ingest of one small markdown file completes with
    write_state {m=Y, q=Y, n=Y, v=Y} on the resulting document
```

---

## When to use this

- Fresh clone on a new Mac Studio / mini / laptop
- After a major macOS or Docker Desktop upgrade where the LaunchAgent
  may have been lost
- Onboarding a teammate to the Apple-side workflow
- Sanity check after changing anything in `scripts/apple_ml_services/`
  or `docker-compose.apple-mlx.yml`

## Reference points if the agent gets confused

- Constraint deep-dive: `GOTCHAS.md § "Apple Silicon hybrid profile"`
- Compose override: `docker-compose.apple-mlx.yml`
- Installer source: `scripts/install_apple_mlx_runtime.sh`
- Model pull: `scripts/pull_apple_mlx_models.py`
- Sidecars: `scripts/apple_ml_services/{embedder_mlx,reranker_mlx,docling_svc}/main.py`
- Smoke: `scripts/smoke_apple_mlx.sh`

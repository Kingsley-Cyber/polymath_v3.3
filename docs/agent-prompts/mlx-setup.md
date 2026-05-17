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

Run the one-shot setup:

  bash scripts/setup_apple_mlx.sh

This will:
    - reject non-Darwin / non-arm64 hosts
    - generate secrets and bootstrap the runtime layout
    - stage code to ~/PolymathRuntime/apple_ml_services/
    - install a uv venv with the pinned requirements
    - pre-pull and verify the two MLX model repos into ~/PolymathRuntime/volumes/hf-cache
    - install the LaunchAgent (auto-restart)
    - bring up Docker with docker-compose.apple-mlx.yml
    - verify backend env wiring
    - smoke /embeddings, /rerank, and /health on ports 8082, 8081, 8500

If you need to run the phases manually instead:
  bash scripts/bootstrap-runtime.sh --generate-secrets --compose-profiles mcp
  bash scripts/install_apple_mlx_runtime.sh
  docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml up -d --build
  bash scripts/smoke_apple_mlx.sh

The reranker ordering check must pass. If it returns zeroes or does not
separate the pattern-related docs from the lemonade doc, the MLX reranker
is not deployed correctly.

Manual env wiring check inside the backend container:
  docker exec polymath_v33-backend-1 sh -c '
    echo EMBEDDER_URL=$EMBEDDER_URL
    echo RERANKER_URL=$RERANKER_URL
    echo DOCLING_URL=$DOCLING_URL
    echo RERANKER_SCORE_SCALE=$RERANKER_SCORE_SCALE
  '
  All four must show host.docker.internal addresses + cosine. If any
  point at the in-cluster service names, the override didn't apply —
  re-run docker compose with both -f flags.

Health check the full stack.
  docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml ps
  All five core services (backend, mongodb, qdrant, neo4j, litellm)
  must be (healthy). Embedder/reranker/docling will NOT appear because
  they're host-native — that's correct.

Constraints you must respect:
  • Never change EMBEDDING_DIMENSION or the embedding model unless
    you also re-ingest every corpus — Qdrant collections are dimension-
    locked.
  • Never push to main; commit on a feature branch.
  • setup_apple_mlx.sh overwrites host sidecar files after backing up prior
    copies under ~/PolymathRuntime/logs/apple_ml_services_backups/. Use
    --preserve-host-sidecars only when the user explicitly wants to keep
    hand-edited host implementations.
  • The reranker MUST run with RERANKER_SCORE_SCALE=cosine. The override
    sets it; do not strip it.
  • Do not use the DeepSeek API directly — only via LiteLLM.
  • Do not commit anything to .env (it's gitignored and contains
    secrets).

What "done" looks like:
  • smoke_apple_mlx.sh passes including the rerank ordering assertion
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
- One-shot setup: `scripts/setup_apple_mlx.sh`
- Installer source: `scripts/install_apple_mlx_runtime.sh`
- Model pull: `scripts/pull_apple_mlx_models.py`
- Runtime smoke: `scripts/verify_apple_mlx_runtime.py`
- Sidecars: `scripts/apple_ml_services/{embedder_mlx,reranker_mlx,docling_svc}/main.py`
- Smoke: `scripts/smoke_apple_mlx.sh`

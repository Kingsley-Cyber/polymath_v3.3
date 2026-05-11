# Apple Silicon (MLX) hybrid profile

## Why this exists

Docker Desktop on macOS cannot pass through the Apple GPU. Containerised
embedder/reranker fall back to CPU and run 10–20× slower than MLX on
Metal. So MLX runs **host-native** on macOS under launchd, and the
Docker stack reaches it over `host.docker.internal`.

## The three-layer setup

| layer | location | what it does |
|---|---|---|
| Docker stack | usual compose | backend, mongo, qdrant, neo4j, litellm, mcp, frontend |
| Host MLX sidecars | `~/PolymathRuntime/apple_ml_services/{embedder_mlx,reranker_mlx,docling_svc}/main.py` | FastAPI services on host ports 8082 / 8081 / 8500 |
| Compose override | `docker-compose.apple-mlx.yml` | disables in-cluster embedder/reranker/docling; retargets backend at `host.docker.internal:{8082,8081,8500}`; sets `RERANKER_SCORE_SCALE=cosine` |

## Bring-up sequence

```bash
# 1. One-time host setup
bash scripts/install_apple_mlx_runtime.sh
# Platform-gates Darwin/arm64. Stages code to ~/PolymathRuntime/.
# Provisions uv venv. Pre-pulls MLX models. Installs LaunchAgent.

# 2. Bring up Docker WITH the override
docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml up -d --build

# 3. Smoke
bash scripts/smoke_apple_mlx.sh
# Asserts reranker ordering. Catches missing projector.
```

## Critical gotchas

### 1. Reranker scaffold returns zeros
The repo ships `reranker_mlx/main.py` as a scaffold — `_load_model()` and
`_score_pairs()` are placeholders. They explicitly `return [0.0] * n`.
The verified Mac Studio implementation builds an `MLPProjector
(mlx.nn.Module)` by hand because `mlx-embeddings` cannot auto-load
Jina v3's quantised projector weights.

**The installer protects the verified host code from rsync clobber**
(commit `8a66b85`). On every re-run, if `~/PolymathRuntime/apple_ml_
services/{embedder,reranker,docling}_mlx/main.py` exists, the rsync
excludes those files. So once your verified code is in place, the
installer is safe to re-run.

The smoke script asserts `score(relevant) > score(irrelevant)` on a
hand-picked trio. If it fails, the projector isn't loaded.

### 2. `RERANKER_SCORE_SCALE=cosine` is mandatory
Jina v3 returns cosine in [0, 1], not logits. The retriever's
negative-logit "low confidence" guard discards every result if this
env is wrong. The compose override sets it; don't strip.

### 3. `wait -n` was a footgun
The supervisor in `scripts/apple_ml_services/start.sh` used to call
`wait -n` (bash 4.3+). macOS ships bash 3.2 at `/bin/bash` and the
LaunchAgent points at `/bin/bash`. Fixed `6077dc4` — uses a PID poll
loop now, bash-3.2 compatible.

### 4. Jina v3 is CC BY-NC 4.0
Fine for personal/research. Not cleared for commercial. If shipping
externally, swap to a licensed alternative.

### 5. CPU-only docling on Mac
Layout parsing runs on Apple Silicon CPU. 3-8× slower than NVIDIA
docling. OCR disabled by policy (`DOCLING_OCR_ENABLED=false`).
Scanned/image PDFs need pre-OCR.

## Agent prompt for fresh Apple Silicon bring-up

`docs/agent-prompts/mlx-setup.md` has the paste-ready prompt for
handing the repo to Claude Code on a new Mac.

## Cross-platform handoff (Windows → Mac Studio)

The user's plan: ingest on Windows (full-precision torch embedder),
export the runtime, import on Mac Studio (MLX mxfp8 embedder).

This works because:
- `Qwen/Qwen3-Embedding-0.6B` (Docker) and `mlx-community/Qwen3-
  Embedding-0.6B-mxfp8` (MLX) have the **same architecture, tokenizer,
  pooling, normalisation, and 1024-dim output**.
- Cosine drift between fp16/fp32 and mxfp8 is ~0.001–0.01 on identical
  inputs. Below the noise floor of top-K retrieval.
- Mongo / Qdrant / Neo4j dumps are OS-neutral.

Export script (`scripts/export-runtime.sh`) auto-selects git-bash GNU
tar on Windows to avoid BSD/GNU tar incompat with macOS. `--smoke`
preflights the round-trip without needing the runtime root.

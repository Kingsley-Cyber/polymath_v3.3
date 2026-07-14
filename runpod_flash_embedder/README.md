# Runpod Flash Embedding Worker

This directory deploys the optional `runpod` burst EMBEDDING lane for
bulk/backfill ingestion (checklist P1.8 "separate ingestion embedder
capacity"). It hosts `Qwen/Qwen3-Embedding-0.6B` — the SAME model as the
local embedder sidecar — on serverless CUDA GPUs so large ingests stop
queueing behind the single local Metal GPU (~7 texts/sec measured).

The worker is stateless. It never receives MongoDB, Qdrant, or Neo4j
credentials. Vectors return to Polymath, where the dispatcher
(`backend/services/embedder.py`, embed mode `runpod`) asserts the dimension
of every row before anything lands in Qdrant.

## Compatibility invariant

Vectors from this worker are written into the same collections as vectors
from the local sidecar, so they MUST be interchangeable: same model, same
pooling (the model repo's sentence-transformers config), same
`normalize_embeddings=True` L2 normalization, and no worker-side prompt
mutation. The backend may submit already-serialized role text; every provider
must encode those bytes unchanged. Document ingestion is raw under the
canonical Qwen3 profile. Dimension is 1024. See the module docstring in
`app.py`. Do not change
`POLYMATH_EMBED_MODEL_ID` unless every other embedding host and the corpus's
frozen `embedding_model_id` change in lockstep.

Interactive/query traffic stays on the local sidecar (`mode="local"`); this
lane is for `embed_batch(mode="runpod")` bulk work only.

## Deploy

```bash
cd runpod_flash_embedder
python -m venv .venv
.venv/bin/pip install runpod-flash
.venv/bin/flash login
RUNPOD_EMBED_MIN_WORKERS=0 \
RUNPOD_EMBED_MAX_WORKERS=8 \
RUNPOD_EMBED_WORKER_CONCURRENCY=1 \
RUNPOD_EMBED_IDLE_TIMEOUT=60 \
RUNPOD_EMBED_SCALER_VALUE=1 \
RUNPOD_EMBED_EXECUTION_TIMEOUT_MS=600000 \
.venv/bin/flash deploy --python-version 3.12
```

Register the resulting endpoint ID on each Runpod account that should serve
embedding (accounts without it are skipped by embed mode `runpod`):

```bash
docker exec -e RUNPOD_ACCOUNT_KEY=... polymath_v33-backend-1 \
    python scripts/register_runpod_account.py \
    --name acct1 --endpoint-id <extraction-ep> --embed-endpoint-id <embed-ep>
```

Multi-account routing reuses the extraction registry: least-in-flight
dispatch across accounts, per-account concurrency semaphores, one-hop
failover (`services/runpod_flash_extraction._AccountDispatcher`).

## Wire contract

`polymath.runpod_embed.v1` — request `{"contract_version", "texts"}` with at
most 256 texts per request (over-cap requests are rejected loudly); response
`{"contract_version", "vectors", "model", "dim", "metrics"}`.

## Knobs and divergences from the extraction worker

GPU preference is identical: L4, then RTX A5000, then RTX 4090. Worker
count, per-worker concurrency, scaler value, idle timeout, and execution
timeout are deploy-time settings (env vars above); redeploy after changing
them. `RUNPOD_EMBED_EXECUTION_TIMEOUT_MS` defaults to 600000 (10 min), not
the extractor's 30 min: an embed request is bounded at 256 texts, so a long
ceiling only turns a wedged worker into a hidden stall.

Start with a one-text canary and compare its cosine similarity against the
local sidecar's vector for the same text (expect ~1.0) before pointing bulk
backfills at this lane.

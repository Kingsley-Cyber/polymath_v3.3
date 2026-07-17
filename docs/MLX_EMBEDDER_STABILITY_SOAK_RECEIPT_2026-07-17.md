# MLX embedder stability and sustained-soak receipt

Date: 2026-07-17

Review branch: `codex/mlx-embedder-stability-20260717`

Base: `3157ec9f76746e2a56bb56b5b3b3fc08a5a9925a`

## Verdict

`MLX-EVAL-STABILITY-V1` is **GREEN** for the client-side Step 0 gate.
The frozen eval specs, request contract, prompts, scoring, and corpus stores
were not changed.

## Root cause and change

- **VERIFIED:** the production local query client already had a 30-second
  timeout, but QueryPlanV2 wrapped every embedding stage in a 5-second
  `asyncio.wait_for`. The two failed evals therefore could not benefit from
  the client timeout.
- **VERIFIED:** the outer embedding deadline is now 30 seconds in Settings,
  compose, and fallback call sites. The query client timeout remains 30
  seconds.
- **VERIFIED:** a local HTTP timeout receives exactly one retry. Existing
  non-timeout transient retries and large-batch splitting remain intact.
- **VERIFIED:** the pooled client keeps idle connections for 120 seconds. A
  new `POST /api/health/embedder/batch-ready` route checks readiness, idle
  state, queue state, prior error state, and frozen dimension; performs one
  fixed neutral inference through that same pool; then rechecks health.
- **VERIFIED:** `scripts/run_eval_with_embedder_preflight.py` launches the
  supplied eval command only after that route returns `status=ready`. It exits
  78 without launching the command if the preflight refuses.

## Receipts

| Gate | Command | Key result | True exit |
|---|---|---|---:|
| Focused + adjacent isolated tests | `docker run ... python -m pytest -q tests/test_embedder_eval_stability.py tests/test_query_embedding_batch.py tests/test_connection_pooling.py tests/test_embedder_warmup.py` | 24 passed | 0 |
| Canonical deployed-container tests | `docker exec -e PYTHONPATH=/app -w /app polymath_v33-backend-1 python -m pytest -q /tmp/mlx-tests/test_embedder_eval_stability.py /tmp/mlx-tests/test_query_embedding_batch.py /tmp/mlx-tests/test_connection_pooling.py` | 15 passed | 0 |
| Live runtime contract | `docker exec ...` plus `curl -X POST http://127.0.0.1:8000/api/health/embedder/batch-ready` | host MLX URL; query timeout 30; outer deadline 30; 1024 dimensions; ready; queue 0 | 0 |
| Sustained soak | `python3 backend/scripts/run_eval_with_embedder_preflight.py --preflight-url http://127.0.0.1:8000/api/health/embedder/batch-ready -- docker exec -e PYTHONPATH=/app -w /app polymath_v33-backend-1 python scripts/soak_local_embedder.py --calls 100 --concurrency 3` | 100/100 successful; 0 failed; 2.839 s wall; 2,113.748 req/min; latency p50 1,416.047 ms, p95 2,665.257 ms, max 2,837.789 ms; all vectors 1024-d | 0 |

The soak latency is end-to-end per submitted task and deliberately includes
semaphore queue time. The wall clock and completion counts are the throughput
authority.

## Data and rollback

- **VERIFIED:** no corpus, Mongo, Qdrant, or Neo4j writes were performed.
- **VERIFIED:** the preflight payload contains no request text or credential.
- **VERIFIED:** the temporary live deployment used immutable image
  `sha256:fddb3eaed71fcbffd593f281307f3be6f10dbf82e2453b92becc22a2254f60c2`.
- Rollback is a canonical five-overlay backend recreate from the shared
  `claude-continuation-20260713` worktree. Step 0.5 remains a separate
  owner-directed parity/routing decision and is not claimed by this receipt.

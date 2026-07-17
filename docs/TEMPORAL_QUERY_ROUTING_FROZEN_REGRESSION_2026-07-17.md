# Temporal query routing frozen regression — 2026-07-17

Status: **GREEN for the requested temporal no-regression gate and
promotion-ready as a default-OFF review branch.** The complete frozen suite
remains honestly RED because the separate, pre-existing negative-refusal gate
is still below its `1.00` target. This receipt does not authorize activation.

Review branch: `codex/temporal-regression-20260717`

Temporal implementation base: `1db5e5b`

MLX stability dependency: `d29e8ae` (cherry-pick of `c74acb9`)

## Immutable contract

| Input | SHA-256 |
|---|---|
| Frozen 17-query preregistration | `8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110` |
| Frozen runner | `b9a1aa940b589d8abff3d1875bb6b362e1e05248116f7fdcc1b2abb487f5e347` |
| 15-document selection | `da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00` |
| Corpus | `2c894530-8d57-4432-a6d4-bc14505a698b` |

Both arms used the immutable request/scoring runner, all three tiers,
`top_k=10`, concurrency `3`, the same image
`sha256:c325ed4902891360defa38f6b84311ecfaf9b368535e15825c14e4a202ce4783`,
and the recorded `anthropic/minimax-m2.7` query route. The safe model-pool hash
was `91bf6ceb54940ac467163624c3a92e2284f28cdbfda3863ccf4acc3671edadfe`;
credential presence was verified without printing or moving the credential.
The only runtime difference was `TEMPORAL_QUERY_ROUTING_ENABLED=false|true`.
The relationship allocator stayed OFF.

Before each scorer command, the Step 0 wrapper required a successful warm
inference through the backend's production pooled client. Both preflights
reported `status=ready`, dimension `1024`, queue depth `0`, query timeout
`30s`, keepalive expiry `120s`, and one bounded timeout retry.

## Frozen OFF/ON result

| Metric | OFF | ON | Target/verdict |
|---|---:|---:|---|
| Executions | 51/51 | 51/51 | PASS |
| Technical success | 1.0000 | 1.0000 | PASS |
| Effective-tier match | 1.0000 | 1.0000 | PASS |
| Corpus-boundary precision | 1.0000 | 1.0000 | PASS |
| Citation membership | 1.0000 | 1.0000 | PASS |
| Direct document hit | 1.0000 | 1.0000 | `>=0.85`, PASS/no regression |
| Lay-language document hit | 1.0000 | 1.0000 | `>=0.75`, PASS/no regression |
| Relationship minimum-distinct | 0.7500 | 0.7500 | `>=0.75`, PASS/no regression |
| Negative fail-closed | 0.5556 | 0.4444 | `1.00`, existing gate remains RED |
| Full frozen scorer | RED | RED | Negative gate only |

All 102 executions completed with no transport or scorer error. OFF wall time
was `455.267s`; ON wall time was `401.959s`.

The only scored row that changed category was
`negative_genomics::qdrant_mongo_graph`, fail-closed `true -> false`.
An isolated detector audit proves this CRISPR/CFTR query has
`active=false` and zero temporal expressions, so the temporal prior did not
fire for that row. The dated wool-tax negative does fire on
`fourteenth-century`, but its per-tier refusal verdicts were identical OFF and
ON. The negative family was RED before this task and remains assigned to the
separate answerability work; it is not concealed or counted as a temporal
improvement.

## Actual command receipts

| Gate | Actual command | Output tail | True exit |
|---|---|---|---:|
| Combined focused/adjacent tests | `docker run --rm ... polymath-temporal-regression-tests:d29e8ae python -m pytest -q tests/test_embedder_eval_stability.py tests/test_query_embedding_batch.py tests/test_connection_pooling.py tests/test_embedder_warmup.py tests/test_temporal_query_routing.py tests/test_rerank_text_hydration.py tests/test_funnel_a_fair_mode.py tests/test_relationship_evidence_feature_flag.py tests/test_planned_retrieval.py` | `63 passed, 16 warnings in 2.57s` | 0 |
| OFF deploy | `docker compose --env-file /Users/king/polymath_v3.3/.env -p polymath_v33 -f docker-compose.yml -f /Users/king/polymath_v3.3/docker-compose.override.yml -f docker-compose.offline-ingest.yml -f docker-compose.apple-mlx.yml -f docker-compose.daily.yml -f tmp/temporal-eval-off.yml up -d --build --force-recreate backend` | backend started; image `c325ed49...` | 0 |
| OFF contract preflight | runtime verification + `/api/health/embedder/batch-ready` + immutable-hash/model-route checks | healthy, dim 1024, temporal false, MiniMax route exact | 0 |
| OFF frozen arm | `python3 backend/scripts/run_eval_with_embedder_preflight.py --preflight-url http://127.0.0.1:8000/api/health/embedder/batch-ready -- docker exec ... python /tmp/e2e_retrieval_eval.py ... --output .../e2e-temporal-regression-off-20260717.json --concurrency 3` | 51/51; technical 1.0; direct 1.0; lay 1.0; relationship 0.75; negative 0.5556 | 1 (expected frozen negative verdict) |
| ON switch | canonical compose files + `tmp/temporal-eval-on.yml up -d --no-deps --force-recreate backend` | same image; temporal true; relationship false | 0 |
| ON contract preflight | same health/hash/model-route checks | healthy, dim 1024, temporal true, MiniMax route exact | 0 |
| ON frozen arm | same immutable wrapper/runner with `...-on-20260717.json` | 51/51; technical 1.0; direct 1.0; lay 1.0; relationship 0.75; negative 0.4444 | 1 (expected frozen negative verdict) |
| Detector audit | isolated branch image, two negative queries | genomics inactive; dated tax query active on `fourteenth-century` | 0 |
| Canonical restore | canonical five overlays at `7233077`, backend build/recreate | healthy; MLX dim 1024; temporal setting absent/default OFF | 0 |

The first isolated test command was uncredited (`EXIT=4`) because tests are
intentionally not baked into the backend image. A second setup attempt was
uncredited (`EXIT=2`) because required test-only settings were absent, and a
third was uncredited (`EXIT=1`) because the host-side warmup module was not
mounted. The credited command mounted the exact tests and sidecar source and
passed 63/63 without changing any test.

## Artifacts and disposition

| Artifact | SHA-256 |
|---|---|
| OFF result journal | `f982c08d13e83a3c4a33ed9043c018d6d09fd17892160e6726779531a0944196` |
| ON result journal | `ca69c773a7a1d5ef739ba9bc3b63ff30ea5d50dbb188f4102512b65e0ef1ac33` |

The durable journals remain under the existing encrypted-runtime ingest volume;
they are not committed because they include answer excerpts. The run used the
read-only retrieval path and performed no corpus mutation. Normal chat
conversation/trace rows may be written by `/api/chat`; these are not corpus
data and are not represented as a zero-database-write claim.

The canonical backend was restored from `/Users/king/polymath_v3.3` at
`7233077` through exactly the canonical five overlays, verified healthy with a
live 1024-dimensional embed, and the eval lock was released. The branch keeps
the temporal feature default OFF. Verdict: **promotion-ready for review and
merge as a dark-shipped feature; activation remains a separate decision.**

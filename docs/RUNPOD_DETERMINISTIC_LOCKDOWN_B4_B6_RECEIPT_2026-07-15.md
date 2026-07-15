# RunPod deterministic extraction B4–B6 receipt — 2026-07-15

Status: **GREEN for B4 synthetic behavior, B5 same-chunk equivalence, and B6
retry safety. B7 real-ingest cutover remains open.**

## Locked surface

- Image: `king2eze/polymath-local-extraction` at immutable manifest
  `sha256:4cb084572687f772cab481adce649cf03c15283368c3541772f85465ee50f896`.
- Runtime profile: `polymath.torch_cuda_deterministic.v1`.
- Source closure:
  `2e47c86fe41db25b3a0fc81408ff775a829be59871a5479a1bfd1a4dad0e8010`.
- Frozen comparison spec:
  `a214bff374e5684e0f4b521eb042d6e253c4b28e07846cd7bfd935ce0cdde6f8`.
- Current retained primary green: `hk81nfl5cnwufx`, template `68bfxhigga`,
  min 0 / max 1. Both legacy extraction blues and the secondary account are
  unchanged.

The RunPod worker is stateless and reports zero MongoDB, Qdrant, or Neo4j
writes. Keys remained inside encrypted backend settings and no receipt emits a
secret value.

## B4/B5 valid same-chunk result

The accepted live canary processed all 12 frozen tasks and returned 12
results, 126 controlled entity mentions, 56 predicates, all required
modalities, one negated predicate, both required temporal expressions, and
zero relations. There were zero missing/extra results and zero exact semantic
mismatches. The maximum GLiNER confidence delta was
`2.384185791015625e-06`, below the unchanged `1e-05` tolerance, and all
threshold-side selections matched.

Accepted live job:
`d53645cd-74b8-4569-a144-5a61d99aa9cc-u1`; delay 99,485ms, execution
12,497ms. Full receipt:
`/tmp/runpod-determinism-live-canary-v2.json`; summary log:
`/tmp/runpod_determinism_live_canary_v2_summary.log`, true `EXIT=0`.

## B4 fail-closed controls

All three preregistered invalid requests reached the exact wrapper and were
classified using the provider's intentional-refusal semantics: terminal
provider `FAILED` plus the exact structured payload
`{success:false,error_code:"extraction_contract_rejected"}`. This status pair
is required for invalid controls only; a valid job with provider `FAILED`
remains a hard failure.

| Control | Job | Warmth at submit | Delay / execution | Result |
|---|---|---|---|---|
| malformed contract | `18b5e861-a2bb-4f26-a9df-d63b0ae46bbc-u1` | not yet instrumented | 628ms / 328ms | exact named refusal |
| out-of-registry label injection | `4c20c5f1-15fb-4d5c-9372-d7018e4abefd-u1` | ready 1, throttled 0 | 9,522ms / 294ms | exact named refusal |
| empty source identity | `347ef356-4a80-4241-bca8-c8faa3a21864-u2` | ready 1, throttled 0 | 387ms / 334ms | exact named refusal |

The final two-control command was:

```text
sh -c 'docker exec -u appuser -e PYTHONPATH=/tmp/runpod-controls-retry-source:/app polymath_v33-backend-1 python /tmp/runpod-controls-retry-source/scripts/run_runpod_green_lockdown.py --mode controls-remaining --green-name polymath-local-extraction-green-3b66f55-deterministic-v1 --baseline /tmp/runpod-controls-source/baseline.json --out /tmp/runpod-controls-remaining-v5.json --job-journal /tmp/runpod-controls-job-journal-v5.jsonl --case-receipt-dir /tmp/runpod-controls-v5-cases > /tmp/runpod_controls_retry_remaining.log 2>&1; echo EXIT=$? >> /tmp/runpod_controls_retry_remaining.log'
```

Actual output tail: `runpod_jobs=2`, `worker_durable_writes=0`,
`secret_values_emitted=0`, both cases `success=false` with exact code, and
`EXIT=0`.

Receipt hashes:

- final result `9d6df662ecd6010f3721a9df38f2f85dbb744fb0575a2c9ed0446fce4a26cd79`;
- fsynced lifecycle journal `771a6525b4e100c65ce73551d82f16e2df760b8144c888755c49188ae09ab65c`;
- case receipts `7983aa531f6f9b076bf969c126b5d2f9d772f8fb9d6f55325b7da721785d72fc`
  and `560a5103d99cd127d7b300e54688b2fd89b9d22a59b500ca2ec5892426098784`.

## B6 identical-request replay

Replay job `ce5835e5-bcdb-463d-9d31-320404824a85-u2` completed independently
with delay 157,028ms and execution 12,649ms. It exactly reproduced semantic
hash `781d22ac130cc40e0f42ae8a8cfa87c9ffc2532aa0b862192625a60a4b024f71`
and confidence-inclusive results hash
`7ba87c12d322b61468527bd07d248f335643533734ca7b18fd767448ffc48d9d`.
Full results were equal, confidence max delta was 0 across 126 mentions, job
IDs were distinct, and durable writes were zero. Compare receipts:
`/tmp/runpod_determinism_b6_replay_compare.log` and
`/tmp/runpod_determinism_b6_semantic_hash.log`, both true `EXIT=0`.

## Failure history and operational boundary

An earlier control-2 attempt never reached a worker, timed out at the former
300-second client patience, was cancelled, and was not interpreted as a
contract verdict. The senior classified it as provider capacity weather and
authorized a transport-only 900-second control timeout plus pre-submit warmth
journaling. Focused tests are 10/10; Black, compile, and diff checks are true
`EXIT=0`. The fresh retry then passed without weakening any refusal or parity
gate.

B7 is not claimed. The published production adapter still constructs and
accepts only legacy `polymath.runpod_gliner_relex.v2/v3`, while the locked
worker requires `polymath.runpod_local_extraction.v1`. No fresh corpus or
production setting was changed. The retained scale-to-zero green awaits the
senior's ruling on the bake plan's additive test-route adapter and cutover
boundary.

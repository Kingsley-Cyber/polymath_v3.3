# T9.3 B1 Phase-2 Telemetry-Drift Recovery Seal Receipt — 2026-07-15

## Verdict

**GREEN prerequisite; no continuation launched.** The authorized recovery
stopped fail-closed at checkpoint 150 because two successful jobs were written
with missing provider-call telemetry. Runtime backend and ingest-worker used a
stale `services/llm.py` that lacked the current response-telemetry seam, while
the paid-pass runtime parity closure did not include that file.

The senior ruled at `COORDINATION.md#2026-07-15T10:09:30Z` to book the three
observed HTTP-200 calls as exact conservative bounds, continue the same
performance recovery, overlay the current wrapper without rebuilding, expand
runtime parity permanently, and add a named fail-closed contract guard before
any continuation claim. This receipt seals the source/runtime prerequisite.
It does not book the two historical rows and does not launch continuation.

## Stopped state and evidence

- Stopped execution: `/tmp/t93_p2_resume_execution_v2.log`, true `EXIT=1`,
  `stop_reason=cost_telemetry_incomplete` at checkpoint 150.
- Durable state: 721 rows = 143 succeeded / 7 DLQ / 571 queued / zero running.
  Ordinals 206 and 207 are succeeded, `cache_hit=false`, with stored accepted
  artifacts; their semantic dispositions are not changed by cost recovery.
- Safe LiteLLM endpoint census:
  `/tmp/t93_p2_resume_v2_litellm_endpoint_timestamps_safe.log`, `EXIT=0`;
  three `/chat/completions` HTTP-200 calls correlate to ordinal 206's initial
  plus repair calls and ordinal 207's single call. No request/response content
  is printed.
- Sealed provider-card bounds:
  `/tmp/t93_p2_resume_cost_bounds_safe.log`, `EXIT=0`; ordinal 206
  `$0.06673898`, ordinal 207 `$0.03819987`, total `$0.10493885`.
- Protected and ambient canonical-store censuses remained exact through the
  stop. Checkpoint 0150 and `resume_execution_v2.json` remain immutable.

## Sealed changes

1. `services/llm.py` exposes the versioned
   `litellm-response-telemetry.v1` wrapper contract.
2. `services/semantic_gateway.py` provides a credential-blind, call-free
   contract receipt. Phase-2 preflight includes it, and execution refuses
   before exact-GO/materialization/claiming under named failure code
   `provider_telemetry_contract_guard` if the required wrapper is absent.
3. A successful non-cache result whose actual provider cost is unavailable
   records `bounded_success_exposure.v1`, the observed one/two-call count, the
   exact reservation upper bound, and the provider-card source. Actual cost
   remains null and `cost_complete` remains false; no cost is guessed.
4. `_cost_accounting` accepts that row only when call count, reservation,
   exposure bound, basis, null actual, incomplete-actual marker, and sealed
   price source all agree. Any mismatch remains fail-closed incomplete.
5. `scripts/verify_semantic_gateway_runtime_parity.py` permanently verifies a
   49-file closure in backend and ingest-worker. It includes the prior 36
   files plus `services/llm.py` and the transport/structured-output dependency
   sweep. Missing files and hash drift fail the process.

## Deployment parity

The pre-overlay verifier correctly returned `EXIT=1` with ten mismatches:
five paths in each canonical container. In addition to the four changed
source files it found a previously missed stale
`registries/structured_output_capabilities.v1.json`. Only those exact files
were copied to both containers using `docker cp`; no image rebuild, process
restart, or provider call occurred. Post-overlay and final parity are GREEN
with 49/49 exact in both containers.

Current SHA-256 values on host, backend, and ingest-worker:

| Path | SHA-256 |
|---|---|
| `services/llm.py` | `3a5425f0be2b400739656a1eb4d8b5f0b7b334f922601b9d91c2341b5d4d3b52` |
| `services/semantic_gateway.py` | `1a0bab32029743e808eb6f49048e017b497f97855e27eabf97e26d9ebe40bc53` |
| `scripts/semantic_gateway_mark_paid_pass.py` | `e50ed63648af5f54d1c6362bd6d68b84c510a3a86752b2adb3c0afa596c61e50` |
| `scripts/semantic_gateway_mark_prose_phase2.py` | `44d544d461cf125be225ad9897f1aa3af2cc5205cc9a1f6e14dc75be085acb49` |
| `registries/structured_output_capabilities.v1.json` | `5ab892e15b9faa9b42fc816dee8cbd8e59d34c88156e479521839d47365e69d8` |

## Gates and true exits

| Gate | Result | Receipt |
|---|---|---|
| Pre-overlay permanent parity | expected RED; 5 drifted paths/container, `EXIT=1` | `/tmp/t93_telemetry_runtime_parity_pre_overlay.log` |
| Exact overlay | five files/container, no rebuild, `EXIT=0` | `/tmp/t93_telemetry_overlay_v1.log` |
| Backend focused tests | 81/81, `EXIT=0` | container `/tmp/t93_telemetry_tests_v2.log` |
| Ingest-worker focused tests | 81/81, `EXIT=0` | container `/tmp/t93_telemetry_tests_v2.log` |
| Black | 9 files unchanged, `EXIT=0` | `/tmp/t93_telemetry_black_check.log` |
| Backend compile + contract | `COMPILE_EXIT=0`, `CONTRACT_EXIT=0` | container `/tmp/t93_telemetry_compile_v2.log` |
| Worker compile + contract | `COMPILE_EXIT=0`, `CONTRACT_EXIT=0` | container `/tmp/t93_telemetry_compile_v2.log` |
| Final 49-file parity | zero mismatches, `EXIT=0` | `/tmp/t93_telemetry_runtime_parity_seal.log` |

The contract probe reports required=observed
`litellm-response-telemetry.v1`, `available=true`, credential read false, and
provider call false in both canonical containers.

## Continuation boundary

Publication of these exact bytes precedes any ledger recovery. The ruled
booking must use compare-and-set guards on ordinals 206/207 and total exactly
`$0.10493885` without modifying semantic/cache status. A fresh credential-
blind continuation preflight must then bind the exact 150-terminal operational
state, preserve the original performance baseline/deadline of 148/198, keep
checkpoint 0150 and stopped execution output immutable, and start its next
checkpoint at 0200. No tail, projection, or activation is authorized here.

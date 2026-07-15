# T9.3 sentence-hybrid v3 runner seal receipt — 2026-07-15

Status: **GREEN; zero credential reads, zero provider calls, zero database
writes.** This receipt seals the runner only. It is not a canary execution
receipt and does not authorize Phase 2.

## Frozen GO boundary

- Authorization: `COORDINATION.md:2026-07-15T03:39:13Z:v3-CANARY-GO`
- Packet set: `sha256:89ace7ede4eab1d00f7f8d062b92d756cc5f7243fe4d0c3d0c7e0fec131b2d43`
- Packet schema: `sha256:5c600d3047807541a09be38d01933b6e048f5a3f730de1b5e2cf6c48991f2e40`
- Selection: `sha256:6aed7b1a967c1ad8889a0f058091e7f47691053d25185ff03cac797b3875f595`
- Exact selected-ten hard authority: `$0.78260930`
- Provider: LongCat Tier 3, `openai/LongCat-2.0`, 8,192 maximum output tokens,
  temperature 0, thinking disabled, prompt v6, repair v3.
- Acceptance: at least 9/10 structurally and semantically accepted, followed
  by strict faithfulness review of every accepted output.

After the credential-blind preflight rederives the live population, the runner
asserts every term above plus the approved population/mapping census before
entering its post-GO database/settings path, attaching encrypted settings,
reading the LongCat credential, materializing jobs, acquiring the paid lane,
or dispatching a provider call. It retains the shared two-attempt reservation
guard and checks the separate `$49.45` cumulative owner umbrella before and
after execution.

## Gate receipts

| Gate | Actual result | True exit receipt |
|---|---|---|
| Host focused tests | 43 passed | `/tmp/t9_3_v3_runner_host_tests.log`, `EXIT=0` |
| Backend canonical focused tests | 43 passed | `/tmp/t9_3_v3_runner_backend_tests_v2.log`, `EXIT=0` |
| Ingest-worker canonical focused tests | 43 passed | `/tmp/t9_3_v3_runner_worker_tests_v2.log`, `EXIT=0` |
| Black | Three files unchanged | `/tmp/t9_3_v3_runner_black.log`, `EXIT=0` |
| Host compile | Passed | `/tmp/t9_3_v3_runner_host_compile.log`, `EXIT=0` |
| Backend canonical compile | Passed | `/tmp/t9_3_v3_runner_backend_compile_v2.log`, `EXIT=0` |
| Ingest-worker canonical compile | Passed | `/tmp/t9_3_v3_runner_worker_compile_v2.log`, `EXIT=0` |
| Diff check | Passed | `/tmp/t9_3_v3_runner_diff.log`, `EXIT=0` |
| Live invalid-GO refusal | `PaidPassError`; runner exit 1 expected by wrapper | `/tmp/t9_3_v3_negative_seal_v3.log`, `EXIT=0` |
| Live refusal state census | 0 phase rows, calls, selections, leases, active ingests, or running jobs | `/tmp/t9_3_v3_negative_seal_state_v3.log`, `EXIT=0` |

Pure tests cover each frozen hash/reference/authority argument, exact umbrella
fit, one-quantum-short umbrella refusal, refusal before runner settings or
credential access, and the existing B4 reservation regression. The first live
negative invocation used nonexistent corpus name `mark` and therefore stopped
earlier on `MaterializationError`; it is superseded by the corrected frozen
corpus invocation above and produced no writes or calls.

## Verdict

VERIFIED: the exact runner is fail-closed at the GO boundary and is safe to
commit before using the senior's existing ten-packet canary authorization.
The canary outcome and faithfulness verdict remain unobserved.

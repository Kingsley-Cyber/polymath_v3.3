# T9.3 B1 Phase-2 Resume Launch-Fix Seal Receipt — 2026-07-15

## Verdict

**GREEN.** The first published resume invocation refused before
materialization, claims, or provider calls because the preflight and execution
Mongo clients represented the same UTC BSON datetimes differently. Preflight
returned aware UTC datetimes while the under-lease execution read returned
naive UTC datetimes; baseline hashing serialized `+00:00` only on the former.
The two reads had identical job IDs, statuses, completion order, instants,
costs, and counts, but produced different hashes.

The runner now canonicalizes both representations to UTC before baseline
hashing. This preserves the already-sealed aware baseline hash
`sha256:d5c7fd3cd86ae961ec71ab5719c79020dbb489530c8bc97ab203bd69f734ab0c`
and makes the under-lease recomputation identical. No selection, prompt,
repair prompt, schema, route, price, budget, checkpoint, quality gate, or
bounded-recovery behavior changed.

The senior ruled at `COORDINATION.md#2026-07-15T09:47:55Z` that the zero-claim,
zero-call refusal did not consume the ONE-resume authorization and approved
an observability-only seam. Execution-stage refusals now emit one allowlisted,
non-secret `error_code` for exact-GO, operational, credential, lane lease,
under-lease baseline, or materialization guards. Failure receipts contain no
exception message, credential, packet text, prompt text, or provider output.

## Failed-launch and state evidence

- Failed launch: `/tmp/t93_p2_resume_execution.log`, true `EXIT=1`; generic
  `PaidPassError`; no checkpoint 0150.
- Post-refusal preflight:
  `/tmp/t93_p2_resume_preflight_after_refusal_safe_v5.log`, `EXIT=0`; exact
  721 rows = 141 accepted / 7 DLQ / 573 queued / zero running; baseline
  `d5c7fd3…`; current cumulative basis `$6.955576299999998`.
- Read-only state isolation:
  `/tmp/t93_p2_execution_guard_readonly_diag.log`, `EXIT=0`; credential
  available, no active ingest, no running semantic job, no lane lease, no
  post-baseline checkpoint, and all 721 `last_planned_at` values unchanged at
  06:00:21Z.
- Root-cause boundary receipt:
  `/tmp/t93_p2_pre_materialization_boundary_diag_v3.log`, expected `EXIT=1`;
  preflight hash `d5c7fd3…`, under-lease hash `c82162c1…`, with the only
  identity-input difference being aware versus naive serialization of the
  same completion instants.

## Sealed invariants

- Selection: 721 exact persisted jobs; selection hash
  `sha256:ee8769280255856fef4f69cd4fbb0d35d3669be661dfc95d60e4281323d711d4`.
- Packet set:
  `sha256:f867e62203c84e29867d129f87a6b019173a657b5cc78c18f9d1b4d143fdc952`.
- Baseline: 148 terminal = 141 accepted / 7 DLQ; rolling ranks 99–148
  contain 44 accepted / 6 failed at ranks 109/118/121/123/126/147.
- Terminal ledger identity:
  `sha256:192ffb6b585f036a1611ec13741a8f2e93d795e5253e3bf426419ef741160617`.
- Rolling identity:
  `sha256:04affd4fa8725fac60c80b662a1cb07911310a7848c102e8a22b51f56ea5a4ed`.
- Fixed absolute authority: `$49.4464896999999995`; current basis
  `$6.955576299999998`; maximum next reservation `$0.09536318`; the resume
  does not refresh the umbrella.
- Only the exact historical red window is latched. Every other stop remains
  live. Recovery must occur by terminal 198; failure to recover or a later
  rolling fall parks for owner.

## Gates and true exits

| Gate | Result | Receipt |
|---|---|---|
| Backend expanded tests | 85/85, `EXIT=0` | `/tmp/t93_p2_resume_fix_backend_tests.log` |
| Ingest-worker expanded tests | 85/85, `EXIT=0` | `/tmp/t93_p2_resume_fix_worker_tests.log` |
| Black | 2 files unchanged, `EXIT=0` | `/tmp/t93_p2_resume_fix_black_v4.log` |
| Backend compile | `EXIT=0` | `/tmp/t93_p2_resume_fix_backend_compile_v2.log` |
| Ingest-worker compile | `EXIT=0` | `/tmp/t93_p2_resume_fix_worker_compile_v2.log` |
| Host/backend/worker parity | 36 files exact, `EXIT=0` | `/tmp/t93_p2_resume_fix_overlay_parity.log` |
| Pre-materialization boundary | both baseline hashes exact; boundary reached; zero writes/calls, `EXIT=0` | `/tmp/t93_p2_resume_fix_boundary_green_v2.log` |
| Wrong exact-GO | expected `EXIT=1`; `error_code=exact_go_guard`; no message | `/tmp/t93_p2_resume_fix_invalid_go_stagecode.log` |
| Final live resume preflight | exact baseline GREEN, `EXIT=0` | `/tmp/t93_p2_resume_fix_preflight_safe_v6.log` |

Runner SHA-256 on host, backend, and ingest-worker:
`937c4b97bfd23a05e24aabd4bf34951b4550b4203e1b4e2eecd4b56d1a2872e0`.

## Failure and rollback posture

The launch fix is a deterministic serialization normalization plus
message-free stage telemetry. It is additive and revertible. The rejected
resume output remains immutable, no checkpoint 0150 exists, and no paid or
canonical state was created by the failed launch or its diagnostics. The
actual relaunch must use a fresh absent execution-output path.

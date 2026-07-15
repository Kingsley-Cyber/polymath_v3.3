# T9.3 B1 Prose Phase-2 Bounded-Resume Seal — 2026-07-15

Status: **GREEN / SEALED FOR THE SENIOR-AUTHORIZED ONE RESUME**.

This receipt seals only the operational recovery seam authorized at
`COORDINATION.md#2026-07-15T09:10:30Z`. The prompt, repair prompt, schema,
provider route, packet shape, 90% rolling threshold, ReadTimeout rule,
consecutive-DLQ rule, budget law, selection, and canonical fence are unchanged.
No provider call, credential read, durable write, projection, or activation
occurred during this seal.

## Exact resume baseline

| Field | Verified value |
|---|---:|
| Selected rows | **721** |
| Terminal | **148** |
| Accepted | **141** |
| Dead letter | **7** |
| Queued | **573** |
| Running | **0** |
| Rolling ranks | **99–148** |
| Rolling accepted / failed | **44 / 6** |
| Rolling failure ranks | **109, 118, 121, 123, 126, 147** |
| Next checkpoint | **150** |
| Recovery deadline | **198 terminal** |

Resume baseline hash:
`sha256:d5c7fd3cd86ae961ec71ab5719c79020dbb489530c8bc97ab203bd69f734ab0c`.

Historical-window identity hash:
`sha256:04affd4fa8725fac60c80b662a1cb07911310a7848c102e8a22b51f56ea5a4ed`.

Terminal-ledger identity hash:
`sha256:192ffb6b585f036a1611ec13741a8f2e93d795e5253e3bf426419ef741160617`.

Selection hash remains
`sha256:ee8769280255856fef4f69cd4fbb0d35d3669be661dfc95d60e4281323d711d4`;
packet-set hash remains
`sha256:f867e62203c84e29867d129f87a6b019173a657b5cc78c18f9d1b4d143fdc952`.

## Recovery state machine

- Only the exact baseline 44/50 historical red window is latched.
- Every other existing stop remains live from the first new claim, including
  cumulative second `ReadTimeout`, five consecutive terminal DLQs, cost
  incompleteness, reservation/budget boundary, credential drift, and
  canonical-store invariance.
- When the last 50 reaches at least 90%, recovery is durably reported and the
  original rolling gate re-enables exactly.
- If recovery is not reached within 50 new terminal rows, terminal 198 stops
  with `rolling_recovery_not_reached_by_terminal_limit` and parks for owner.
- If recovery occurs and the rolling window later falls below 90%, the runner
  stops with `rolling_acceptance_below_90_percent_after_recovery` and parks
  for owner.
- Claims are bounded so the recovery phase cannot overshoot terminal 198.
- Checkpoint numbering starts at 150. Existing 0050/0100 and the failed
  execution JSON are never overwritten. Resume output must be a new absent
  path and cannot be named `execution.json`.

The first attempt's generic fresh-selection equation is now naturally false:
141 of its rows became certified. Resume therefore uses the persisted exact
721-row selection identity, not a counterfactual fresh selection from the
post-purchase ledger. This distinction is explicit in the preflight; it does
not add, remove, or repurchase a parent.

## Budget seal

| Field | Verified value |
|---|---:|
| Original prior basis | `$2.7564896999999995` |
| Current cumulative ceiling basis | `$6.955576299999998` |
| Original remaining authority | `$46.69` |
| Fixed absolute ceiling | `$49.4464896999999995` |
| Remaining under fixed ceiling | `$42.4909134000000015` |
| Maximum next reservation | `$0.09536318` |

The resume does **not** refresh the `$46.69` umbrella. The fixed absolute
ceiling from the original launch remains authoritative.

## Frozen provider contract

- Model: `openai/LongCat-2.0`, tier 3, `max_tokens=8192`, temperature 0,
  thinking disabled.
- Prompt hash:
  `sha256:ee523bbf674d26a3974488e48fdfae6f0f4a4238e1df94ce39067dc9d35c10eb`.
- Repair-prompt hash:
  `sha256:0d4d7d5f50c0a98312cf4052510aa4225d1cc235b319df84c5eacf1c5801d145`.
- Schema hash:
  `sha256:ce106660a46ff7799e79399816dd634645e1b906f80905db3460f70787f97c99`.
- Credentials remain encrypted in Mongo settings; resume preflight reports
  `credential_plaintext_read=false`.

## Gates and true exits

| Gate | Result | Receipt |
|---|---|---|
| Backend expanded gateway/runner/reservation tests | 79/79, `EXIT=0` | `/tmp/t93_p2_resume_expanded_tests_v3.log` |
| Worker expanded gateway/runner/reservation tests | 79/79, `EXIT=0` | `/tmp/t93_p2_resume_worker_tests_v6.log` |
| Black | 2 files unchanged, `EXIT=0` | `/tmp/t93_p2_resume_black_final.log` |
| Backend compile | `EXIT=0` | `/tmp/t93_p2_resume_compile_backend_final.log` |
| Worker compile | `EXIT=0` | `/tmp/t93_p2_resume_compile_worker_final.log` |
| Host/backend/worker overlay parity | 36 files exact, `EXIT=0` | `/tmp/t93_p2_resume_overlay_hash_guard_v4.log` |
| Immutable execution-output collision refusal | expected `EXIT=1`; SHA unchanged | `/tmp/t93_p2_resume_collision_refusal.log` |
| Wrong baseline exact-GO refusal | expected `EXIT=1` | `/tmp/t93_p2_resume_invalid_go.log` |
| Post-refusal live preflight | baseline identical, `EXIT=0` | `/tmp/t93_p2_resume_post_invalid_safe.log` |
| Final credential-blind resume preflight | GREEN, `EXIT=0` | `/tmp/t93_p2_resume_preflight_safe_v4.log` |

The runner source hash in both containers is
`bf1395edce1ea9d59ac055c76f4417613e130661b2944a301b5fc887ea2f76b9`.

## Actual live-preflight command and output tail

```bash
sh -c 'docker exec -e PYTHONPATH=/app -w /app polymath_v33-backend-1 python scripts/semantic_gateway_mark_prose_phase2.py --mode resume-preflight --checkpoint-dir /tmp/t93_prose_phase2_run --out /tmp/t93_prose_phase2_run/resume_preflight_v4.json > /tmp/t93_p2_resume_live_preflight_v4.log 2>&1; rc=$?; echo EXIT=$rc >> /tmp/t93_p2_resume_live_preflight_v4.log; exit $rc'
```

Safe inspection tail:

```text
"all_green": true,
"selection_count": 721,
"baseline": {"all_green": true, "terminal_count": 148,
"accepted_count": 141, "failure_count": 7, "queued_count": 573,
"running_count": 0, "baseline_hash": "sha256:d5c7fd3…",
"rolling_window": {"completion_rank_min": 99,
"completion_rank_max": 148, "accepted_count": 44,
"failure_count": 6,
"failure_completion_ranks": [109,118,121,123,126,147]}},
"current_cumulative_ceiling_basis_usd": "6.955576299999998",
"absolute_authorized_ceiling_usd": "49.4464896999999995",
"resume_does_not_refresh_umbrella": true,
"active_ingest_batches": 0, "running_semantic_jobs": 0,
"provider_calls": 0, "database_writes": 0, "canonical_writes": 0
EXIT=0
```

Immutable prior-execution collision refusal:

```text
"error_class": "ResumeOutputCollision", "all_green": false
BEFORE=a902153e7f9fe02c371e1246719e842e8181c58b78b46eaf4969ecebfeb263ce
AFTER=a902153e7f9fe02c371e1246719e842e8181c58b78b46eaf4969ecebfeb263ce
EXIT=1
```

## Preserved red preflight evidence

- Initial compile and Black attempts hit root-owned container cache paths;
  redirected compile/Black reruns are green on the exact same bytes.
- The first expanded test run exposed a missing queue-integrity overlay, not a
  source failure; after exact overlay it is green.
- Live resume preflight first failed closed on a missing parent-eligibility
  registry overlay, then on missing route/price cards. Both were zero-provider,
  zero-database-write failures and led to complete two-container parity.
- The first fully loaded live preflight reported RED because it reused the
  fresh-selection equation after 141 purchases. The corrected contract
  explicitly proves the persisted exact selection and still reports the fresh
  equation as false; no denominator or selection was changed.

## Evidence labels

- **VERIFIED:** exact code/tests, baseline identities/counts/ranks, frozen
  hashes, budget arithmetic, output collision guard, invalid-GO refusal,
  two-container parity, zero-provider/write state, and true exits above.
- **INFERRED:** none.
- **ASSUMED:** none.

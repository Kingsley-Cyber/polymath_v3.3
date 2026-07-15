# T9.3 B1 Prose Phase-2 Rolling-Stop Failure Receipt — 2026-07-15

Status: **RED / CORRECTLY STOPPED AT THE REGISTERED ROLLING GATE**.

This receipt closes only the first Phase-2 execution attempt. It does not
claim paid-pass completion, tail eligibility, projection, retrieval
activation, or production readiness. The runner stopped because the last 50
terminal completions fell below the frozen 90% acceptance floor.

## Final settled ledger

| Field | Verified value |
|---|---:|
| Exact selected population | **721** |
| Terminal | **148** |
| Accepted | **141** |
| Dead letter | **7** |
| Queued / unclaimed | **573** |
| Running | **0** |
| Provider calls | **206** |
| Overall attempted acceptance | **95.270270%** |
| Last-50 accepted / DLQ | **44 / 6** |
| Last-50 rolling acceptance | **88%** |
| Stop reason | `rolling_acceptance_below_90_percent` |
| Execution green | `false` |
| Runner exit | **1** |

The overall attempted acceptance does not override the registered rolling
authority. At 44/50, the rolling window is below 90%, so the correct result is
RED even though 141/148 terminal rows were accepted. No new row was claimed
after the stop. The single already-running request settled before this final
receipt was generated.

The immutable execution report is
`/tmp/t93_prose_phase2_run/execution.json`, SHA-256
`a902153e7f9fe02c371e1246719e842e8181c58b78b46eaf4969ecebfeb263ce`.
Only checkpoints 0050 and 0100 were emitted; the stop occurred before a 0150
checkpoint.

## Completion-order failure ledger

| Completion rank | B1 ordinal | Durable parent | Failure | Provider calls | Cost treatment |
|---:|---:|---|---|---:|---:|
| 69 | 125 | `209d3863…_parent_0006` | `latent_concepts` | 2 | `$0.03602445` actual |
| 109 | 164 | `2ea6852b…_parent_0007` | `ReadTimeout` | 0 reported | `$0.06` bound |
| 118 | 175 | `30cf4973…_parent_0011` | `attempt_limit_exhausted` | 2 | `$0.05372535` actual |
| 121 | 178 | `333dd5a6…_parent_0002` | `attempt_limit_exhausted` | 2 | `$0.05825830` actual |
| 123 | 179 | `333dd5a6…_parent_0003` | `latent_concepts` | 2 | `$0.03553185` actual |
| 126 | 183 | `333dd5a6…_parent_0007` | `HTTPStatusError` | 0 reported | `$0.06` bound |
| 147 | 204 | `419a49a6…_parent_0006` | `latent_concepts` | 2 | `$0.03302280` actual |

The six failures in the final 50 are completion ranks
109/118/121/123/126/147. They comprise one `ReadTimeout`, two
`attempt_limit_exhausted`, two `latent_concepts`, and one `HTTPStatusError`.
The first structural failure at rank 69 is outside the final rolling window.
No raw provider output or HTTP body was read or printed.

## Cost and authorization

| Scope | Known actual | Bounded exposure | Ceiling basis |
|---|---:|---:|---:|
| This Phase-2 attempt | `$4.079086600000001` | `$0.12` | `$4.199086600000001` |
| Corpus-wide cumulative | `$6.775576299999998` | `$0.18` | `$6.955576299999998` |

The two unpriced Phase-2 transport outcomes are each conservatively bounded
at `$0.06`. The third corpus-wide bound predates this attempt. Budget
accounting is complete-with-bounded-exposure and remains below the absolute
cumulative guard `$49.4464896999999995`; this is not a budget stop.

## Re-buy and canonical state

- Replacement ordinal 60 (`b4_atomic:60`) succeeded. Its source payload is
  preserved, source cache is non-serving and faithfulness-rejected, and one
  append-only supersession row exists.
- Replacement ordinal 570 (`b4_atomic:569`) remains queued. It has no
  supersession row and its source cache is unchanged. The execution report
  therefore honestly records one present/inserted supersession row versus two
  expected re-buys.
- `canonical_store_census.scope.v2` is valid. Protected stores are exactly
  unchanged and no ambient Qdrant change was observed.
- Security fields state `canonical_write=false`, no packet text, no raw
  provider output, and no plaintext credential in the receipt.

## Actual commands and output tails

The published execute command used the exact sealed selection, contract
hashes, authorization reference, prior basis, remaining authority, and output
paths. Its immutable log ended with the complete execution record and the
true runner exit:

```text
"terminal_count": 148, "accepted_count": 141, "dead_letter_count": 7,
"provider_call_count": 206, "execution_green": false,
"stop_reason": "rolling_acceptance_below_90_percent"
EXIT=1
```

Final safe execution inspection command:

```bash
sh -c 'docker exec polymath_v33-backend-1 python /tmp/t93_p2_stop_safe.py final > /tmp/t93_p2_final_stop_inspect.log 2>&1; rc=$?; echo EXIT=$rc >> /tmp/t93_p2_final_stop_inspect.log; exit $rc'
```

```text
"durable_queue": {"accepted_count": 141, "dead_letter_count": 7,
"provider_call_count": 206, "terminal_count": 148},
"execution_green": false,
"stop_reason": "rolling_acceptance_below_90_percent"
EXIT=0
```

Final rolling-window inspection command:

```bash
sh -c 'docker exec -e PYTHONPATH=/app -w /app polymath_v33-backend-1 python /tmp/t93_p2_dlq_safe.py rolling > /tmp/t93_p2_rolling_stop_final.log 2>&1; rc=$?; echo EXIT=$rc >> /tmp/t93_p2_rolling_stop_final.log; exit $rc'
```

```text
"rolling_window_count": 50, "rolling_accepted_count": 44,
"rolling_dead_letter_count": 6, "rolling_acceptance": 0.88,
"status_counts": {"dead_letter": 7, "queued": 573, "succeeded": 141},
"terminal_count": 148,
"stop_reason": "rolling_acceptance_below_90_percent"
EXIT=0
```

Final re-buy-state inspection command:

```bash
sh -c 'docker exec -e PYTHONPATH=/app -w /app polymath_v33-backend-1 python /tmp/t93_p2_rebuy_safe.py > /tmp/t93_p2_rebuy_stop_state.log 2>&1; rc=$?; echo EXIT=$rc >> /tmp/t93_p2_rebuy_stop_state.log; exit $rc'
```

```text
"source_namespace": "b4_atomic:60", "replacement": {"ordinal": 60,
"status": "succeeded"}, "supersession_ledger": {"history_preserved": true}
"source_namespace": "b4_atomic:569", "replacement": {"ordinal": 570,
"status": "queued"}, "supersession_ledger": null
EXIT=0
```

Execution JSON integrity command:

```bash
docker exec polymath_v33-backend-1 sha256sum /tmp/t93_prose_phase2_run/execution.json
```

```text
a902153e7f9fe02c371e1246719e842e8181c58b78b46eaf4969ecebfeb263ce  /tmp/t93_prose_phase2_run/execution.json
```

## Decision boundary

The senior accepted this stop and preregistered the next decision before any
diagnosis: publish this receipt, then inspect failures by document, failure
classes/provider health over completion order, the next approximately 50
queued ordinals, and accepted-row cost/latency stability. One unchanged-gate
resume is permitted only if evidence shows content-driven clustering without
a provider-health trend. Repeated HTTP errors or latency drift parks the pass
for owner visibility. A second later rolling stop also parks the pass. Hard
document failures remain honest losses; no gate, prompt, schema, provider
contract, or tail policy changes.

## Evidence labels

- **VERIFIED:** all counts, identities, failure classes, costs/bounds,
  canonical/security fields, log exits, and the execution-file hash above are
  read from the durable ledger or immutable receipts.
- **INFERRED:** none in this failed-stop receipt; causal interpretation is
  deliberately deferred to the separate read-only diagnosis.
- **ASSUMED:** none.

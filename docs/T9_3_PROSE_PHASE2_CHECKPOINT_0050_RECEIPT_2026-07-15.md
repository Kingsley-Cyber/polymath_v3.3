# T9.3 B1 Prose Phase-2 Checkpoint 0050 Receipt — 2026-07-15

Status: **GREEN / PAID PASS CONTINUES**.

This is the exact 50-terminal boundary emitted by the published Phase-2
runner. It is an interim receipt, not a final-pass or activation claim.

## Verified boundary

| Field | Verified value |
|---|---:|
| Generated at | `2026-07-15T06:50:20Z` |
| Selection | `mark-phase2.b1-interim-prose.parent-digest.v6.v2` |
| Terminal | **50** |
| Accepted | **50** |
| Acceptance | **100%** |
| Dead letter | **0** |
| Read timeout | **0** |
| Current concurrency | **3** |
| Stop reason | `null` |

The runner had one in-flight request at the checkpoint. Cost accounting was
therefore complete with bounded exposure: known cumulative actual cost
`$3.9383889499999993`, one unpriced in-flight request bounded at `$0.06`, and
cumulative ceiling basis `$3.9983889499999994`. The absolute authorized
ceiling remains `$49.4464896999999995`.

The checkpoint's `canonical_store_census.scope.v2` record is valid and
exactly unchanged for protected stores. It reports no ambient Qdrant change.
The security record is also explicit: `canonical_write=false`; no packet
text, raw provider output, or plaintext credential is present in the receipt.

## Actual command and output tail

```bash
sh -c 'docker exec polymath_v33-backend-1 python -c '\''import json; r=json.load(open("/tmp/t93_prose_phase2_run/checkpoint_0050.json")); c=r["canonical_store_census"]; out={"schema_version":r["schema_version"],"generated_at":r["generated_at"],"selection_name":r["selection_name"],"terminal_count":r["terminal_count"],"accepted_count":r["accepted_count"],"acceptance":r["acceptance"],"dead_letter_count":r["dead_letter_count"],"read_timeout_count":r["read_timeout_count"],"current_concurrency":r["current_concurrency"],"stop_reason":r["stop_reason"],"absolute_authorized_ceiling_usd":r["absolute_authorized_ceiling_usd"],"cumulative_cost":r["cumulative_cost"],"canonical_scope_version":c["scope_version"],"canonical_scope_valid":c["scope_valid"],"canonical_exactly_unchanged":c["exactly_unchanged"],"canonical_protected_exactly_unchanged":c["protected_exactly_unchanged"],"ambient_change_observed":c["ambient_change_observed"],"security":r["security"]}; print(json.dumps(out,sort_keys=True))'\'' > /tmp/t93_p2_checkpoint_0050_inspect.log 2>&1; echo EXIT=$? >> /tmp/t93_p2_checkpoint_0050_inspect.log'
```

```text
{"absolute_authorized_ceiling_usd": "49.4464896999999995", "acceptance": 1.0, "accepted_count": 50, "ambient_change_observed": false, "canonical_exactly_unchanged": true, "canonical_protected_exactly_unchanged": true, "canonical_scope_valid": true, "canonical_scope_version": "canonical_store_census.scope.v2", "cumulative_cost": {"actual_cost_complete": false, "bounded_exposure_usd": 0.06, "budget_accounting_complete": true, "ceiling_basis_usd": 3.9983889499999994, "cost_accounting_state": "complete_with_bounded_exposure", "known_actual_cost_usd": 3.9383889499999993, "unpriced_exposure_count": 1}, "current_concurrency": 3, "dead_letter_count": 0, "generated_at": "2026-07-15T06:50:20Z", "read_timeout_count": 0, "schema_version": "polymath.semantic_digest_prose_phase2_checkpoint.v1", "security": {"canonical_write": false, "packet_text_in_receipt": false, "plaintext_credentials_in_receipt": false, "raw_provider_output_in_receipt": false}, "selection_name": "mark-phase2.b1-interim-prose.parent-digest.v6.v2", "stop_reason": null, "terminal_count": 50}
EXIT=0
```

## Labels

- **VERIFIED:** every value above comes from the immutable checkpoint file and
  the true-exit inspection command.
- **INFERRED:** none.
- **ASSUMED:** none.

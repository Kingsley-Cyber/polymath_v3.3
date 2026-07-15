# T9.3 B1 Prose Phase-2 Checkpoint 0100 Receipt — 2026-07-15

Status: **GREEN / PAID PASS CONTINUES AT CONCURRENCY 3**.

This is the exact 100-terminal boundary emitted by the published Phase-2
runner. It is an interim receipt, not a final-pass or activation claim.

## Verified boundary

| Field | Verified value |
|---|---:|
| Generated at | `2026-07-15T07:49:33Z` |
| Selection | `mark-phase2.b1-interim-prose.parent-digest.v6.v2` |
| Terminal | **100** |
| Accepted | **99** |
| Dead letter | **1** |
| Acceptance | **99%** |
| Read timeout | **0** |
| Current concurrency | **3** |
| Stop reason | `null` |

The first 100 terminal rows are not 100/100 accepted, so the preregistered
concurrency escalation condition is not satisfied. The runner correctly
remains at concurrency 3. The one dead letter is the already-receipted
structural `latent_concepts` validation failure at B1 prose ordinal 125; it is
not a transport failure and does not form a consecutive-DLQ streak.

The runner had one in-flight request at checkpoint emission. Cost accounting
was therefore complete with bounded exposure: known cumulative actual cost
`$5.3598194999999995`, one unpriced in-flight request bounded at `$0.06`, and
cumulative ceiling basis `$5.419819499999999`. The absolute authorized ceiling
remains `$49.4464896999999995`.

The checkpoint's `canonical_store_census.scope.v2` record is valid and
exactly unchanged for protected stores. It reports no ambient Qdrant change.
The security record is explicit: `canonical_write=false`; no packet text, raw
provider output, or plaintext credential is present in the receipt.

## Actual command and output tail

```bash
sh -c 'docker exec polymath_v33-backend-1 python -c '\''import json; r=json.load(open("/tmp/t93_prose_phase2_run/checkpoint_0100.json")); c=r["canonical_store_census"]; out={"schema_version":r["schema_version"],"generated_at":r["generated_at"],"selection_name":r["selection_name"],"terminal_count":r["terminal_count"],"accepted_count":r["accepted_count"],"acceptance":r["acceptance"],"dead_letter_count":r["dead_letter_count"],"read_timeout_count":r["read_timeout_count"],"current_concurrency":r["current_concurrency"],"stop_reason":r["stop_reason"],"absolute_authorized_ceiling_usd":r["absolute_authorized_ceiling_usd"],"cumulative_cost":r["cumulative_cost"],"canonical_scope_version":c["scope_version"],"canonical_scope_valid":c["scope_valid"],"canonical_exactly_unchanged":c["exactly_unchanged"],"canonical_protected_exactly_unchanged":c["protected_exactly_unchanged"],"ambient_change_observed":c["ambient_change_observed"],"security":r["security"]}; print(json.dumps(out,sort_keys=True))'\'' > /tmp/t93_p2_checkpoint_0100_inspect.log 2>&1; rc=$?; echo EXIT=$rc >> /tmp/t93_p2_checkpoint_0100_inspect.log; exit $rc'
```

```text
{"absolute_authorized_ceiling_usd": "49.4464896999999995", "acceptance": 0.99, "accepted_count": 99, "ambient_change_observed": false, "canonical_exactly_unchanged": true, "canonical_protected_exactly_unchanged": true, "canonical_scope_valid": true, "canonical_scope_version": "canonical_store_census.scope.v2", "cumulative_cost": {"actual_cost_complete": false, "bounded_exposure_usd": 0.06, "budget_accounting_complete": true, "ceiling_basis_usd": 5.419819499999999, "cost_accounting_state": "complete_with_bounded_exposure", "known_actual_cost_usd": 5.3598194999999995, "unpriced_exposure_count": 1}, "current_concurrency": 3, "dead_letter_count": 1, "generated_at": "2026-07-15T07:49:33Z", "read_timeout_count": 0, "schema_version": "polymath.semantic_digest_prose_phase2_checkpoint.v1", "security": {"canonical_write": false, "packet_text_in_receipt": false, "plaintext_credentials_in_receipt": false, "raw_provider_output_in_receipt": false}, "selection_name": "mark-phase2.b1-interim-prose.parent-digest.v6.v2", "stop_reason": null, "terminal_count": 100}
EXIT=0
```

## Labels

- **VERIFIED:** every value above comes from the immutable checkpoint file,
  the safe first-DLQ inspection, and the true-exit inspection command.
- **INFERRED:** none.
- **ASSUMED:** none.

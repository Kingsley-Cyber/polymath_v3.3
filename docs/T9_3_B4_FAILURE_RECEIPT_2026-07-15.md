# T9.3 Lane B B4 Failure Receipt — 2026-07-15

Status: **FAILED / claims-only v2 rejected with evidence**. Phase 2, the owner
sample window, and every further paid dispatch remain sealed.

## 5Ws + how

- **Who:** owner-authorized Lane B, senior-governed, executed by Codex through
  the certified LongCat Tier-3 gateway route.
- **What:** frozen ten-packet `semantic_parent_packet.atomic_claims.v2` B4
  canary, two packets from each preregistered byte/rank band.
- **When:** 2026-07-15 UTC under the senior GO at
  `COORDINATION.md:2026-07-15T01:31:30Z:B4-GO`.
- **Where:** mark corpus `5a20bc21-95df-42c2-80c8-f927b4e83904`; purchased
  artifacts stayed in noncanonical Mongo gateway/job stores.
- **Why it failed:** the packet shape triggered a deterministic empty-tool-
  arguments provider mode in 5/10 selected rows, two accepted outputs failed
  strict faithfulness, and the runner lacked a pre-claim cost reservation.
- **How it was contained:** packet 9 was allowed to terminalize after its
  durable claim, packet 10 was never claimed, protected canonical stores were
  verified byte/count-equivalent by `canonical_store_census.scope.v2`, and a
  universal two-attempt reservation control was added before any future claim.

## Frozen identities and actual command

- Packet population hash:
  `sha256:00960dbeb9d1704421a79ea1abd3b71112e316c66143b2cfe507c709c624bf04`.
- Selection hash:
  `sha256:55ab1e846c40ef2e3a233a01f3333758b9660451b3237241f1976e271d9f203f`.
- Provider: `openai/LongCat-2.0`, Tier 3, `max_tokens=8192`, temperature 0,
  thinking disabled, prompt `parent-digest.v6`, repair prompt v3.
- Command (host paths and full hashes redacted in the log header, not values
  needed to reproduce the identity):

```text
docker compose run --rm --no-deps -T -v HOST_BACKEND:/app \
  -v HOST_TMP:/receipts -w /app backend sh -lc \
  'PYTHONPATH=/app python scripts/semantic_gateway_mark_atomic_b4.py \
  [full senior-authorized hashes] --max-authorized-cost-usd 0.42995425 \
  --out /receipts/t9_3_b4_atomic_paid_receipt.json'
```

Actual output and true exit are in `/tmp/b4_atomic_paid_execution.log`:
`EXIT=1`. The safe structured receipt is
`tmp/t9_3_b4_atomic_paid_receipt.json` and contains no packet text, raw
provider body, or plaintext credential.

## Gate results

| Gate | Result | Key numbers | EXIT |
|---|---:|---|---:|
| Paid B4 execution | FAIL | 4 accepted, 5 DLQ, 1 queued/unclaimed; 15 calls | 1 |
| Preregistered acceptance | FAIL | within authority 3/8; final durable 4/9; required >=9/10 | 1 |
| Strict faithfulness | FAIL | 2/4 valid digests pass; all-ten review cannot close | 0 (read-only diagnosis) |
| Failure classification | PASS | structural 5; semantic 0; transport 0; cap 0; unpriced 0 | 0 |
| DLQ body shape | PASS | 10/10 attempts are zero-byte `empty_tool_arguments` | 0 |
| Hard ceiling | FAIL | actual `$0.45429295`; authority `$0.42995425`; overage `$0.02433870` | 1 |
| Canonical isolation | PASS | Mongo semantic 0; Qdrant 1,364,159; Neo4j 1,361,818 / 3,712,432; all unchanged | 0 |

The diagnosis receipt is `/tmp/b4_atomic_private_diagnosis_v3.stderr`
(`EXIT=0`). The shape classifier is
`/tmp/b4_dlq_shape_classification_v2.log` (`EXIT=0`). Failed setup attempts are
retained rather than hidden: the first diagnosis hit historical-ledger drift,
the next required timezone-aware BSON, and the first shape classifier imported
non-baked operator scripts. None wrote state or called a provider.

## Packet and band evidence

| Frozen band | Rows | Accepted | DLQ | Unclaimed | Actual cost |
|---|---:|---:|---:|---:|---:|
| q00–q25 | ord407, ord87 | 0 | 2 | 0 | `$0.12820310` |
| q25–q50 | ord328, ord60 | 2 | 0 | 0 | `$0.04612830` |
| q50–q75 | ord516, ord102 | 1 | 1 | 0 | `$0.12495705` |
| q75–q90 | ord682, ord275 | 0 | 1 | 1 | `$0.06396955` |
| top decile | ord397, ord569 | 1 | 1 | 0 | `$0.09103495` |

Execution order was ord60 success/one call; ord87, ord102, ord275 structural
DLQ/two calls; ord328 success/one; ord397 and ord407 structural DLQ/two;
ord516 repaired success/two; ord569 success/one after the breached claim;
ord682 never claimed.

All five DLQs ended with structural `json_invalid` at EOF. Their ten stored
raw-output hashes are identical. Read-only inspection of the bodies establishes
the exact shape class without disclosing text: all ten are empty strings, zero
characters and zero UTF-8 bytes. This is neither a semantic-validator kill, a
refusal form, a malformed JSON prefix, nor a size-band effect.

## Faithfulness evidence

- PASS: ord328 and ord516; summary and thesis are supported by emitted claims.
- FAIL: ord60; it elevates automatic negative thoughts to the “key obstacle”
  and internal processes to “central” personal effectiveness without emitted
  support, and reads as claim-list prose.
- FAIL: ord569; “sustainable strategy” and “unique product selection” are
  unsupported synthesis.
- No verdict is possible for five empty-output DLQs or uncalled ord682.

The provider-facing claims-only v2 packet is therefore rejected independently
by structural reliability, accepted-output faithfulness, and the 10/10 success
of the earlier prose packet canary. Its materialized local atomic claims remain
valid candidate evidence and are not deleted or rewritten.

## Ceiling defect and mandatory control

The approved authority covered one capped call per packet, but the gateway can
make a base call plus one repair. The runner also compared only current basis
against authority, so it claimed ord569 at `$0.42732505`; any next call crossed
the ceiling. Exact overage `$0.02433870` is classified
`ceiling_guard_missing_reservation` and counts against the standing `$49.45`
umbrella.

The corrected invariant is:

```text
max_claim_cost = 1.10 * 2 * (
  packet_input_token_upper_bound * input_rate
  + max_output_tokens * output_rate
) / price_unit_tokens

claim only if current_ceiling_basis + max_claim_cost <= authorized_ceiling
```

The shared helper ceiling-rounds to `$0.00000001`; serial and concurrent paid
paths reserve before claim, and the concurrent path reserves cumulatively.
The boundary regression proves exact equality is allowed and one quantum
inside the next-call envelope is denied without calling the claim function.
Host and isolated canonical suites are 57/57 green; Black, compile, and diff
checks are green. Receipt pointers are recorded in `COORDINATION.md` at
2026-07-15T02:47:25Z.

## Disposition

- Phase 2: sealed.
- Owner sample/veto window: did not fire.
- Claims-only v2 retry: prohibited.
- Canonical writes/projections: none.
- Next eligible work: senior review of the sentence-anchored v3 design note.
- Any future call requires all four: reservation control, corrected authority,
  approved v3, and a fresh preregistered canary GO.

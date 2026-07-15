# T9.3 B1 Prose Phase-2 Launch Seal Receipt — 2026-07-15

Status: **GREEN / SEALED FOR OWNER-AUTHORIZED EXECUTION**. This receipt proves
the launch population, certified provider contract, budget guard, durable
identity, pause controls, and zero-provider refusal boundary. It does not claim
that the 721-parent purchase has executed or passed.

## Authority and immutable contract

- Owner relay: `COORDINATION.md` at `2026-07-15T05:24:44Z`.
- Senior execution order: `COORDINATION.md#2026-07-15T05:24:45Z`.
- Corpus: `markbuildsbrands_transcripts`, discovered live by name; the runner
  does not hardcode its corpus ID.
- Contract: certified LongCat Tier 3, `openai/LongCat-2.0`, `max_tokens=8192`,
  temperature 0, thinking disabled, `parent-digest.v6`,
  `parent-digest-repair.v3`, interim-prose packet shape.
- Prompt hash:
  `sha256:ee523bbf674d26a3974488e48fdfae6f0f4a4238e1df94ce39067dc9d35c10eb`.
- Repair-prompt hash:
  `sha256:0d4d7d5f50c0a98312cf4052510aa4225d1cc235b319df84c5eacf1c5801d145`.
- Schema hash:
  `sha256:ce106660a46ff7799e79399816dd634645e1b906f80905db3460f70787f97c99`.
- Credentials remain encrypted in Mongo settings and were not read during the
  preflight.

## Exact population

The credential-blind live preflight closed the following set arithmetic:

| Quantity | Count |
|---|---:|
| B1-eligible parents | 795 |
| Durable exclusion union before re-buy | 76 |
| Rejected-v2 parent re-buys restored | 2 |
| Fresh non-rebuy parents | 719 |
| Exact Phase-2 target | **721** |

The exclusion union is set-exact, not a subtraction of independently
overlapping counts. Live disclosure found 75 B1-eligible attempted/purchased
parents, 52 certified interim-prose parents, and 20 structured-canary selected
parents; their union is 76. The structured-selection category includes the
unclaimed B4 selection row, so it cannot silently re-enter the bulk purchase.

The two re-buys are resolved from the exact accepted B4 durable rows, never by
ordinal coincidence:

- `b4_atomic:60` maps to B1 interim-prose replacement ordinal 60.
- `b4_atomic:569` maps to B1 interim-prose replacement ordinal 570.
- The unrelated `prose989:60` ReadTimeout is a different durable parent and
  remains in the later five-parent tail.

Selection hash:
`sha256:ee8769280255856fef4f69cd4fbb0d35d3669be661dfc95d60e4281323d711d4`.

Selected packet-set hash:
`sha256:f867e62203c84e29867d129f87a6b019173a657b5cc78c18f9d1b4d143fdc952`.

## Budget and dispatch law

- Prior cumulative ceiling basis: `$2.7564896999999995`.
- Owner/senior remaining umbrella: exactly `$46.69`.
- Absolute cumulative guard: `$49.4464896999999995`.
- Full-selection two-attempt worst case: `$56.48863913`.
- Maximum next-claim reservation: `$0.09536318`.

The full worst case is deliberately disclosed as larger than the remaining
umbrella. This matches the senior order: select all 721 but claim only while
`current cumulative basis + next two-attempt reservation <= absolute guard`.
If the boundary is reached, the runner stops with outstanding parents and
surfaces the arithmetic. Actual/bounded terminal telemetry replaces each
reservation before another dispatch.

## Operational controls

- New content-addressed job identities isolate this selection from historical
  superseded, prose, B4, and v3 jobs.
- One durable claim per parent; one gateway attempt envelope with at most two
  provider calls (initial plus targeted repair).
- Completion-order rolling acceptance pause below 90% over 50 terminal rows.
- Pause after five consecutive terminal DLQs.
- Pause at two `ReadTimeout` terminal outcomes.
- Concurrency 3 initially; concurrency 6 only after the first 100 terminal
  rows are 100/100 accepted.
- Exact checkpoints at terminal counts 50, 100, and every later multiple of
  50; each contains census-scope, cost, acceptance, timeout, and stop state.
- `canonical_write=false`; protected Mongo/Qdrant/Neo4j stores are checked
  with `canonical_store_census.scope.v2`.
- Successful re-buys create append-only noncanonical supersession ledger rows,
  preserve the rejected v2 payload/history, and mark those two source cache
  rows non-serving.

## Gates and true exits

| Gate | Result | Receipt |
|---|---|---|
| Initial focused runner tests | 14/14, `EXIT=0` | `/tmp/t93_p2_test_initial_v2.log` |
| Durable-parent identity correction | 15/15, `EXIT=0` | `/tmp/t93_p2_test_identity_fix.log` |
| Corrected live preflight | GREEN, `EXIT=0` | `/tmp/t93_p2_live_preflight_v3.log` |
| Expanded gateway/runner/reservation tests | 75/75, `EXIT=0` | `/tmp/t93_p2_backend_focused_v2.log` |
| Black check | 6 files unchanged, `EXIT=0` | `/tmp/t93_p2_black_check_pre_v2.log` |
| Invalid-GO refusal | expected `PaidPassError`, `EXIT=1` | `/tmp/t93_p2_invalid_go.log` |
| Post-refusal zero-write re-preflight | exact hashes/counts/basis, `EXIT=0` | `/tmp/t93_p2_invalid_go_postcheck.log` |

Two preflight failures remain part of the audit trail rather than being
relabeled: the first caught the invalid cross-namespace ordinal assumption;
the second honestly disclosed that full worst-case exposure exceeds the
umbrella and prompted correction of an over-strong gate to the senior's
per-dispatch reservation law. The expanded test gate also caught an invalid
hash namespace before live mutation. No provider call, credential read, job
materialization, cache write, supersession, or canonical mutation occurred in
any failed or green seal gate.

## Open execution receipts

- Paid launch and exact-N durable materialization.
- First-50 checkpoint and subsequent rolling checkpoints.
- Final 721-row ledger or boundary-stop ledger.
- Three readable accepted digests for the owner record.
- Distinct five-parent tail only after corpus-wide certified acceptance is at
  least 95%.
- No projection or retrieval activation is authorized by this seal.

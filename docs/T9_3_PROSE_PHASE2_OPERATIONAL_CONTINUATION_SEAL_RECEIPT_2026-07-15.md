# T9.3 B1 Phase-2 Operational Continuation Seal Receipt — 2026-07-15

## Verdict

**GREEN; continuation not yet launched.** The two telemetry-drift successes
have been cost-reconciled exactly under the senior's ruling, and a distinct
operational-continuation contract now binds the resulting 150-terminal state.
The original performance recovery remains unchanged: its baseline is terminal
148, its deadline is terminal 198, only the historical red rolling window is
latched, and every nonrolling stop remains live.

The senior ruled at `COORDINATION.md#2026-07-15T10:09:30Z` that the telemetry
drift was an infrastructure stop rather than a performance outcome, so the
same recovery continues after a source/test seal. Publication of this exact
seal is the final prerequisite before launch. No tail, projection, or
activation is authorized.

## Bounded-success reconciliation

The zero-write preflight `/tmp/t93_p2_bounded_success_preflight.log` returned
true `EXIT=0` and proved that ordinals 206/207 still matched their exact
stopped states, both accepted caches existed, and the provider-card bounds
recomputed exactly.

The authorized compare-and-set then returned true `EXIT=0` in
`/tmp/t93_p2_bounded_success_apply.log`:

| Ordinal | Observed calls | Bounded exposure |
|---|---:|---:|
| 206 | 2 | `$0.06673898` |
| 207 | 1 | `$0.03819987` |
| **Total** | **3** | **`$0.10493885`** |

Both rows retain `actual_cost_usd=null` and `cost_complete=false`; the event is
certain but actual cost is not guessed. The operation matched and modified
exactly 2/2 rows. Semantic identity remained byte-identical before/after at
`sha256:7f8f3e750b4d41cf893209aea463b864f8dce0e062fe22e80ea48be6eaff4533`.
Protected canonical stores and ambient Qdrant were exactly unchanged.

Cumulative accounting now closes with:

- known actual cost: `$6.775576299999998`;
- bounded exposure: `$0.28493884999999997` across five rows;
- ceiling basis: `$7.060515149999998`;
- accounting state: `complete_with_bounded_exposure`.

## Exact continuation baseline

The final credential-blind live preflight is GREEN at
`/tmp/t93_p2_continuation_preflight_final.log`, true `EXIT=0`:

- selection: exact persisted 721 rows; selection hash `sha256:ee8769280255856fef4f69cd4fbb0d35d3669be661dfc95d60e4281323d711d4`;
- packet set: `sha256:f867e62203c84e29867d129f87a6b019173a657b5cc78c18f9d1b4d143fdc952`;
- current state: 150 terminal = 143 accepted / 7 DLQ; 571 queued; zero running;
- continuation baseline: `sha256:a8f21ed25b3ebdba6946432d73f9ac5b576b7dee347b5d5e69b1696647f406f1`;
- terminal identity: `sha256:1ad27941f69cc09645be5b6721e9761b1ac23d2c61ee68a89265b04bb921fd5c`;
- rolling ranks 101–150: 44 accepted / 6 failed; failures remain at completion ranks 109/118/121/123/126/147;
- original recovery baseline: terminal 148, `sha256:d5c7fd3cd86ae961ec71ab5719c79020dbb489530c8bc97ab203bd69f734ab0c`;
- recovery deadline: terminal 198; consumed new terminals: 2; next writable checkpoint: 200;
- remaining under fixed absolute ceiling: `$42.3859745500000015`; maximum next reservation `$0.09536318`; umbrella is not refreshed.

The preflight reports zero credential read, provider calls, DB writes, and
canonical writes. The versioned telemetry contract is available and exact.

## Immutable stopped receipts

- `/tmp/t93_prose_phase2_run/checkpoint_0150.json`:
  `3370b7bf80decdcba90b3351918e8bb1c30c206b9c3065671797e620909314ab`.
- `/tmp/t93_prose_phase2_run/resume_execution_v2.json`:
  `ffaa6a224d361f7f94eeeaea6b8f33d6261ba92bf70e90029209d86ee9c9883d`.
- No checkpoint 0200 or later exists.

The continuation preflight and under-lease guard require these exact hashes.
The runner initializes the next checkpoint at 0200, so it cannot overwrite
0150. A continuation execution must use a new absent output path.

## Sealed behavior

1. New `resume-continuation-preflight` and `resume-continuation` modes are
   separate from the original 148-terminal resume contract.
2. Exact-GO binds both authorization references, the new continuation ruling,
   selection/packet/prompt/schema identities, current cost basis, continuation
   baseline, checkpoint 0150, and the stopped execution receipt.
3. Under the lane lease, all identities and immutable file hashes are
   recomputed before materialization.
4. `ProsePhase2ResumeControl` retains original performance baseline 148 and
   deadline 198 while carrying continuation baseline provenance and next
   checkpoint 200.
5. Missing telemetry still refuses under named
   `provider_telemetry_contract_guard` before any continuation claim.

Current runner SHA-256 on host, backend, and ingest-worker:
`30968e82504b72330c059c0e6f937ad9d6ec3d5e1966dd234ea0ab3f2cd1a880`.

## Gates and true exits

| Gate | Result | Receipt |
|---|---|---|
| Bounded-success zero-write preflight | exact two rows/caches/bounds, `EXIT=0` | `/tmp/t93_p2_bounded_success_preflight.log` |
| Bounded-success compare-and-set | 2/2 updated; semantic/canonical invariant, `EXIT=0` | `/tmp/t93_p2_bounded_success_apply.log` |
| Backend focused tests | 83/83, `EXIT=0` | container `/tmp/t93_continuation_tests.log` |
| Ingest-worker focused tests | 83/83, `EXIT=0` | container `/tmp/t93_continuation_tests.log` |
| Black | 2 files unchanged, `EXIT=0` | `/tmp/t93_continuation_black.log` |
| Backend compile | `EXIT=0` | container `/tmp/t93_continuation_compile.log` |
| Worker compile | `EXIT=0` | container `/tmp/t93_continuation_compile.log` |
| Host/backend/worker parity | 49 files exact, `EXIT=0` | `/tmp/t93_continuation_runtime_parity_seal.log` |
| Initial live continuation preflight | exact 150-terminal baseline, `EXIT=0` | `/tmp/t93_p2_continuation_preflight.log` |
| Wrong continuation baseline | expected `error_code=exact_go_guard`, message-free `EXIT=1` | `/tmp/t93_p2_continuation_invalid_go.log` |
| Post-invalid-GO preflight | identical baseline/state, `EXIT=0` | `/tmp/t93_p2_continuation_preflight_after_invalid_go.log` |
| Exact-GO under-lease boundary | reaches materialization guard with zero job materialization/provider/canonical writes, `EXIT=0` | `/tmp/t93_p2_continuation_boundary_probe.log` |
| Final live continuation preflight | exact baseline/state, `EXIT=0` | `/tmp/t93_p2_continuation_preflight_final.log` |

## Launch boundary

After this seal is committed and dual-pushed, the same authorized recovery may
launch once using `resume-continuation`, every exact-GO value above, checkpoint
directory `/tmp/t93_prose_phase2_run`, and a fresh absent execution output.
Failure to recover by terminal 198 or a later rolling fall parks for owner.
Any operational failure remains fix-and-diagnose under the standing contract.

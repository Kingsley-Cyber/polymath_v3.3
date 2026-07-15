# T9.3 B1 Phase-2 Recovery Pause Receipt — 2026-07-15

## Verdict

**PARKED safely.** The recovered Phase-2 digest pass stopped with zero jobs
running on the live second-cumulative-ReadTimeout guard. The owner subsequently
made the remaining digest queue owner-gated and put the fresh 15-document
RunPod extraction E2E ahead of all other work
(`COORDINATION.md#OWNER-RELAY-2026-07-15T10:55:37Z`). Purchased results are
retained; 535 queued rows remain durable and untouched.

The owner also locked the intended architecture: RunPod serves deterministic
extractions (GLiNER/spaCy/Python, scale-to-zero GPU), while the certified API
gateway serves inexpensive LLM summaries/digests. This pause does not change
that architecture.

## Final durable state

- Exact selection: 721 rows.
- Terminal: 186 = 178 accepted / 8 DLQ.
- Remaining: 535 queued / zero running.
- Overall attempted acceptance: 178/186 = 95.70%.
- Post-continuation-baseline performance: 35/36 accepted = 97.22%.
- Final rolling window: 48/50 = 96%; no rolling second-stop.
- Recovery: explicitly reached at terminal 159; original rolling gate was
  re-enabled.
- Consecutive terminal failures: zero.
- Stop reason: `read_timeout_recurrence_pause`.

The continuation execution receipt is
`/tmp/t93_prose_phase2_run/resume_execution_v3.json`, SHA-256
`eec2db4f7f1fa840ac97586534aa53c50fa8cf8beb343641ff2bc2c6051970e0`.
Runner log `/tmp/t93_p2_resume_continuation_v3.log` has true `EXIT=1`, as
required for the live stop.

## Timeout evidence

There are exactly two cumulative `ReadTimeout` terminal rows:

| Completion rank | Ordinal | Packet bytes | Attempt | Bounded exposure |
|---:|---:|---:|---:|---:|
| 109 | 164 | 17,198 | 1 | `$0.06` |
| 185 | 242 | 16,182 | 1 | `$0.06` |

Ordinal 242 is the new stop event. It made no priced call, has
`cost_complete=false`, and is conservatively represented under
`bounded_transport_exposure.v1`. It is not retried here.

Read-only diagnosis:
`/tmp/t93_p2_continuation_stop_diag.log`, true `EXIT=0`.

## Accounting and invariance

- Known actual cost: `$7.691670599999999`.
- Six bounded exposures: `$0.34493884999999996`.
- Complete ceiling basis: `$8.036609449999998`.
- Fixed absolute authorized ceiling: `$49.4464896999999995`.
- Budget accounting: complete with bounded exposure.
- Protected Mongo/Qdrant/Neo4j canonical stores: exactly unchanged.
- Ambient Qdrant only: `hermes_memories` +2, disclosed separately.
- Checkpoint 0150 remains immutable at SHA
  `3370b7bf80decdcba90b3351918e8bb1c30c206b9c3065671797e620909314ab`.
- No checkpoint 0200 exists.

## Materialization incident and restore

At continuation entry, the generic materializer saw accepted caches for the
two ruled bounded-success rows (ordinals 206/207) and temporarily rewrote them
as zero-cost cache hits. This understated cumulative basis by `$0.10493885`.
While the affected rows were unowned and three separate claims were in flight,
the existing senior cost-booking authority was used to exact-CAS restore only
those two bounded rows and their original cache-hit identity. The operation
matched/modified 2/2, made zero provider calls, and restored complete budget
accounting before the runner's next claim decision.

Receipts:

- detection: `/tmp/t93_p2_continuation_safe_poll_002.log`, `EXIT=0`;
- exact restore:
  `/tmp/t93_p2_restore_bounded_success_after_materialize.log`, `EXIT=0`;
- restored-state verification:
  `/tmp/t93_p2_continuation_safe_poll_003.log`, `EXIT=0`.

Claude acknowledged the handling at `COORDINATION.md#2026-07-15T10:43:02Z`.
The source preservation fix is locally drafted but is **not sealed, committed,
or deployed**; it is parked. Any later digest resumption is owner-gated and
requires that fix to pass tests and seal first.

## Owner stop compliance and next priority

The owner stop entry became visible to the executor after the runner had
already stopped on its own guard. At discovery, the state was already zero
running, so no request was interrupted and no unknown provider outcome was
created. No further digest claim, retry, overlay, rebuild, or relaunch occurs.

The next and only priority is the owner's E2E finish line:

1. post the RunPod extractor bake design note;
2. bake current extraction contracts with certified local pins;
3. blue-green deploy and synthetic canary;
4. same-chunk RunPod versus pinned-local validation;
5. ingest 15 deterministic source PDFs into a new corpus only;
6. run the full extraction/embedding/graph/API-summary pipeline;
7. evaluate preregistered retrieval targets.

Existing corpora remain untouched. The remaining Phase-2 digest queue may be
resumed only with a new owner line.

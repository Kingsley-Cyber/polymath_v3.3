# RunPod B4/B5 Instrumented Canary and Parity Failure Receipt — 2026-07-15

Status: **RED on frozen confidence parity; dependent chain stopped and rolled
back.** Functional extraction semantics passed, but this is not a production-
readiness or parity pass.

## Scope

- Corrected private image index:
  `sha256:c03416dcc6ae1fed6fd8851e09360711edea62ccd731c0f22d80653a51edbef1`.
- Temporary primary green endpoint: `l8l0ckyjnfzm9m`; template
  `m58ojy4pru`; workers 0..1; CUDA minimum 13.0.
- Frozen same-chunk suite: 12 preregistered tasks, baseline SHA
  `7615ad23cf7750cc3cb6691aa7c34dbeba8ba891da50141b09d520f85fb5d590`,
  confidence absolute tolerance `1e-5`.
- Protected surfaces: both extraction blues, secondary account, all corpora,
  provider settings, Mongo, Neo4j, and Qdrant.

## Harness correction and diagnostic-only orphan

The first post-redeploy harness invocation submitted a valid job, then failed
before polling because its journal path was inside a root-owned overlay:

```text
PermissionError: /tmp/runpod-green-lockdown/job-journal.jsonl
EXIT=1
Receipt: /tmp/runpod_b4_fix_canary.log
```

No result from that uninstrumented job counts toward a bar. The endpoint/logs
were retained until provider health moved from one queued/initializing job to
completed=1 with worker ready=1, idle=1, unhealthy=0. This is diagnostic-only
evidence that the corrected image boots and serves. The provider did not expose
cost or worker-seconds through the health response, and the harness did not
retain that orphan's ID; no unsupported cost claim is made.

The permanent runner fix appends and `fsync`s a `journal_preflight` event
before HTTP submission. An unwritable journal now refuses with provider call
count zero. Focused suite: 6/6; Black and actual-container-user writable-path
preflight: true `EXIT=0`. Published commit: `ac8bc4a`.

## Instrumented valid job

Journal receipt:

```json
{"event":"journal_preflight","endpoint_id":"l8l0ckyjnfzm9m","case":"valid_same_chunk"}
{"event":"submitted","job_id":"50ded71a-63c9-49da-8a95-c94e411f0a1a-u1"}
{"event":"terminal","status":"COMPLETED","delay_time_ms":1780,"execution_time_ms":1196}
```

The instrumented provider job completed and its full output was preserved
before rollback. Functional canary validation is green:

| Measure | Result |
|---|---:|
| chunks | 12 |
| entities | 126 |
| predicates | 56 |
| modalities | asserted, hypothetical, possible, recommended |
| negated predicates | 1 |
| required temporal phrases | 2/2 |
| relations | 0 |
| missing/extra results | 0 |
| semantic mismatches under frozen comparator | 0 |
| threshold-side selection match | true |

Diagnostic receipt:
`/tmp/runpod_b4_fix_live_canary_validation_diag.log`, true `EXIT=0`.

## Frozen parity failure

The same job fails the preregistered confidence gate:

| Measure | Result |
|---|---:|
| frozen absolute tolerance | `0.00001` |
| maximum absolute delta | `0.0001373291015625` |
| confidence values | 126 |
| values above tolerance | 81 |
| values above `0.0001` | 7 |
| median absolute delta | `0.000015914440155029297` |

The maximum is the `measured baseline` mention in
`child:runpod-lockdown:long_window_overlap`: pinned local
`0.693781852722168` vs green `0.6939191818237305`. The runner stopped before
the three invalid-control jobs. Exact command outcome:

```text
AssertionError: green confidence delta 0.0001373291015625 exceeds tolerance 1e-05
EXIT=1
Receipt: /tmp/runpod_b4_fix_instrumented_canary.log
```

Full status/output preservation:
`/tmp/runpod_b4_fix_failed_parity_job_status.json`, status probe true
`EXIT=0`. Confidence diagnostic:
`/tmp/runpod_b4_fix_confidence_parity_diag.log`, true `EXIT=0`.

The likely mechanism is GPU-versus-local floating-point variation because all
discrete selections and semantic fields are stable. That mechanism is
**INFERRED**, not verified by a second live job. The bar failure and numbers
above are **VERIFIED**. The frozen tolerance is not weakened or redefined.

## Mandatory rollback

```text
Green delete:
deleted_green_id=l8l0ckyjnfzm9m; green_remaining=0; blue_unchanged=true
EXIT=0
/tmp/runpod_b4_fix_abort_delete.log

Primary embed restore:
k695blmk52oscm workersMax 1 -> 2; ID/template/min/idle/scaler/GPU/CUDA unchanged
EXIT=0
/tmp/runpod_b4_fix_embed_capacity_restore_1_to_2.log

Final census:
new green=[]; primary embed max=2; both extraction blues unchanged;
secondary untouched; secret_values_emitted=0
EXIT=0
/tmp/runpod_b4_fix_abort_final_census.log
```

Only inert exact-digest templates and private registry-auth records remain;
they allocate no workers. No corpus or canonical-store write occurred.

## Decision

B4 functional semantics are observed green, but the required combined B4/B5
parity gate is RED. B6 retry, provider cutover, fresh 15-document E2E, and
retrieval evaluation do not start. Any change to the frozen tolerance or
comparison contract requires explicit owner/senior respecification; it cannot
be made as an executor fix.

Subsequent senior ruling authorizes one narrow remediation/retest before any
respecification discussion: bake the versioned deterministic runtime profile,
prove its settings in-image, rebuild/publish privately, and run one new canary
against the unchanged `1e-5` tolerance. This addendum does not alter the RED
result recorded above.

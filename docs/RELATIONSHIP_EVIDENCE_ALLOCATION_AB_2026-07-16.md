# Relationship Evidence Allocation — Frozen OFF/ON Receipt

Date: 2026-07-16 (America/Denver; execution completed 2026-07-17 UTC)

## Verdict

**The requested acceptance gates are green.** With the settings flag enabled,
the relationship minimum-distinct rate improved from **75% to 100%**. Direct
and lay-language document-hit rates remained **100%**, above their respective
85% and 75% floors. The committed default remains OFF.

The complete frozen suite still reports `passed=false` because the pre-existing
negative-refusal gate is 33.33% in both arms. This change did not alter prompts,
scoring, eval specifications, or negative-query behavior.

## Implementation receipt

- Source commits `ea4b348`, `8755976`, and `9f8cfd8` are ancestors of the
  working branch (`git merge-base --is-ancestor`: EXIT=0 for each). The existing
  allocator was retained and reconciled with the current `QueryPlanV2` seam.
- `RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED` is settings-driven and defaults
  to `False`.
- Eligibility reuses the shared query-semantics operator classification and
  requires an active plan with at least two lanes plus `relationship` or
  `comparison`; no second raw-query detector was added.
- The optional LLM decomposer, lane-support allocation, and protected per-doc
  cap are all inside the same dark-launch boundary.
- Restored runtime receipt: flag=`False`; live Qwen3 embedding dimension=1024;
  backend runtime verification EXIT=0.

## Frozen A/B contract

- Corpus: `2c894530-8d57-4432-a6d4-bc14505a698b`
- Frozen preregistration SHA-256:
  `8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110`
- Selection SHA-256:
  `da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00`
- Query model contract in both arms: `anthropic/minimax-m2.7` through
  `opencode-go-anthropic`; safe pool hash
  `91bf6ceb54940ac467163624c3a92e2284f28cdbfda3863ccf4acc3671edadfe`.
- Each arm executed all 51 frozen cases (17 queries × 3 tiers), serially, with
  51/51 technical success and no row errors.

## Scores

| Gate | Flag OFF | Flag ON | Required | Verdict |
|---|---:|---:|---:|---|
| Relationship minimum-distinct rate | 75.00% | **100.00%** | >=75% | PASS |
| Direct document-hit rate | 100.00% | **100.00%** | >=85% | PASS, no regression |
| Lay-language document-hit rate | 100.00% | **100.00%** | >=75% | PASS, no regression |
| Technical success | 100.00% | **100.00%** | 100% | PASS |
| Corpus-boundary precision | 100.00% | **100.00%** | 100% | PASS |
| Citation-source membership | 100.00% | **100.00%** | 100% | PASS |
| Negative refusal (existing unrelated gate) | 33.33% | **33.33%** | 100% | RED, unchanged |

Relationship counts below are distinct expected documents in
`qdrant_only / qdrant_mongo / qdrant_mongo_graph` order.

| Preregistered relationship query | Flag OFF | Flag ON | Per-query result |
|---|---:|---:|---|
| Shoot/edit/emotion | 3 / 3 / 3 | 3 / 3 / 3 | PASS → PASS |
| Fight/camera direction | 1 / 1 / 1 | **2 / 2 / 2** | FAIL → PASS |
| VFX/story pipeline | 3 / 3 / 3 | 3 / 3 / 3 | PASS → PASS |
| Movement/machine/figure | 2 / 2 / 2 | 2 / 2 / 2 | PASS → PASS |

The improved fight/camera query adds the missing *Directing — Film Techniques
and Aesthetics* side while preserving *Stage Combat Arts* in all three tiers.

## Verification and data-safety receipts

- Canonical backend container focused + adjacent suite: **80 passed**, 7 known
  Pydantic namespace warnings, EXIT=0. This includes the inherited allocator
  assertions, feature-flag contract, shared detector tests, evidence-plan tests,
  coverage tests, and latency guards.
- Frozen classifier fixture: 17/17 expected eligibility decisions, EXIT=0.
- OFF artifact SHA-256:
  `b306c6420fb186b30791d289d76d534cfcb5f05af637639571d201c7b1f9b06c`.
- ON artifact SHA-256:
  `c4d9b79c94bcd2defd49837e02d8b9f89c5a10da7f2013abc8c7e32053e28930`.
- Read-only corpus census before/after matched for every substantive Mongo,
  Qdrant, and Neo4j component. Canonical content-only digest in both snapshots:
  `82580d560f5ae341841052b9972587764ed4d3c6809792216be0b523f8f1bdcb`.
  The sole full-snapshot difference was Mongo `ingest_scheduler_state`, a
  runtime scheduler heartbeat changed by container recovery—not corpus data.
- Host inference timeout overrides and all temporarily stopped services were
  restored. Final backend flag=False and all compose services are running.

Durable raw artifacts remain under
`/data/ingest-files/runpod-job-journals/e2e-relationship-evidence-{off,on}-final.json`.

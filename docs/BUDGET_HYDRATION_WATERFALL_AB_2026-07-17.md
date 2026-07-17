# Budget Hydration Waterfall — Serialized Live A/B Receipt

Date: 2026-07-17
Review branch: `codex/router-waterfall-integration-20260717`
Integration commits: `2dfefc4`, `11b1d43`
Corpus: `2c894530-8d57-4432-a6d4-bc14505a698b`

## Verdict

**VERIFIED — quality preservation GREEN; live tier-coverage incomplete.**

The waterfall OFF and ON arms each completed all six immutable bridge
queries technically and produced identical ordered top-three documents for
every query. Both scored 4/6 because the already-settled, independently RED
router baseline fails the camera-motion and character-motion cases. The
waterfall did not add a regression.

The direct packet diagnostic produced identical ON packet hashes on repeated
runs for all six queries. Every packet stayed under its 4,000-token budget.
However, the live queries exercised only the `full` hydration tier: 17 full,
zero summary, and zero skip decisions. Summary/skip behavior is test-proven
but not live-proven by this diagnostic. The flag therefore remains default
OFF and is not recommended for enablement until a preregistered high-pressure
context set exercises both lower tiers.

Router and decomposition remained OFF in both arms because Router A's
promotion verdict was RED. This isolates the waterfall change.

## Build and tests

Exact review image:
`polymath-router-waterfall@sha256:c51494deb84772dce043ac89449cefc73bae03a22b1128045468cda60c608e80`.

| Gate | Result |
|---|---:|
| exact image build | `EXIT=0` |
| combined router/waterfall focused tests | 62 passed |
| focused-test wrapper | `EXIT=0` |
| committed router flag default | OFF |
| committed decomposition flag default | OFF |
| committed waterfall flag default | OFF |

The combined tests covered the four-lane router, Tier-0 routing, waterfall
allocator, packet assembly, hydration mode, rerank hydration, identity
hydration, and document-artifact contracts.

## Live contract

- Immutable selection SHA-256:
  `da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00`.
- Query route: `anthropic/minimax-m2.7`.
- Retrieval tier: `qdrant_mongo_graph`.
- Final top K: 10.
- Eval lock owner:
  `codex/router-waterfall-integration-20260717`.
- Both arms passed the real-inference MLX pre-batch probe at 1,024
  dimensions.
- No corpus writes were authorized.

## Quality A/B

The immutable runner's `arm` field remains `off` in both artifacts because
that field describes the four-lane router state. Waterfall state was changed
only by the separately recorded runtime environment.

| Query | Waterfall OFF | Waterfall ON | Ordered top three equal |
|---|---:|---:|---:|
| camera motion/story | fail | fail | yes |
| sequence continuity | pass | pass | yes |
| staging/blocking | pass | pass | yes |
| visual power/vulnerability | pass | pass | yes |
| character motion | fail | fail | yes |
| storyboard/dramatic through-line | pass | pass | yes |
| **Total** | **4/6** | **4/6** | **6/6** |

Each arm had technical success 6/6. Each immutable runner returned
`EXIT=1` because its upstream suite target is 6/6. The dedicated OFF/ON
comparison of query ID, technical status, pass status, and ordered top-three
documents returned `EXIT=0`.

## Packet and hydration decisions

The direct probe ran each question once through legacy assembly and twice
through waterfall assembly. Determinism applies to the two flagged calls;
the separate legacy retrieval call is not claimed to have identical
candidates.

| Query | Legacy chunks | Packet items | Used / budget tokens | Full | Summary | Skip | Repeated hash |
|---|---:|---:|---:|---:|---:|---:|---|
| camera motion/story | 3 | 7 | 2,396 / 4,000 | 3 | 0 | 0 | `beed98811149338d` |
| sequence continuity | 3 | 7 | 1,991 / 4,000 | 3 | 0 | 0 | `eaa0cca71ba887d4` |
| staging/blocking | 1 | 2 | 913 / 4,000 | 1 | 0 | 0 | `4cf47a105deb3ff5` |
| visual power/vulnerability | 3 | 5 | 737 / 4,000 | 2 | 0 | 0 | `783af3c5ae7b8478` |
| character motion | 5 | 8 | 2,033 / 4,000 | 5 | 0 | 0 | `9fe61b7ea09abc9e` |
| storyboard/dramatic through-line | 3 | 8 | 1,896 / 4,000 | 3 | 0 | 0 | `15ca38deb1f9019f` |
| **Total** | **18** | **37** | **9,966 / 24,000** | **17** | **0** | **0** | **6/6 stable** |

The packet probe returned `EXIT=0`. There were zero spilled tokens, zero
skipped parents, and zero promoted summaries.

## Integrity and restoration

The full read-only store fingerprint changed only because
`ingest_scheduler_state` advanced:

- before:
  `52e30069eec692a35c9054586d51ce35181aace9c25616ec7336e7d0a4bac4a5`;
- after:
  `f4af46f0f32b7cc88d0b010d61da8dbf3a6907ba7bde919390650c3d53e09315`.

After removing the scheduler heartbeat and capture timestamp, the complete
Mongo, Qdrant, and Neo4j comparison returned `EXIT=0`.

The canonical backend was restored from `/Users/king/polymath_v3.3` with
exactly its five canonical overlays and image
`sha256:bf79df8914b73fe50c3c52d2d8cccbbf9870167c42502009262b355218c385a3`.
Router, decomposition, and waterfall variables are absent from the canonical
environment and resolve false/default-OFF. Restoration and corrected flag
validation both returned `EXIT=0`; the eval lock was then released.

One initial fingerprint invocation supplied an unsupported `--corpus-id`
argument and failed before reading stores (`EXIT=2`). The corrected invocation
changed only the harness arguments and completed `EXIT=0`. One initial
post-restore validation probe accessed review-only settings attributes on the
older canonical image and raised `AttributeError`; a fail-safe `getattr`
validation then proved every effective flag false and every variable absent,
`EXIT=0`.

## Cost

The direct packet diagnostic made no synthesis calls. The API quality A/B
made 12 small MiniMax synthesis calls. Chat-lane invoice telemetry is still
the separately queued P7 gap. Applying the existing conservative
two-attempt envelope to six executions per arm gives at most $0.30695 per arm
and $0.61390 combined; this is an upper bound, not an invoice.

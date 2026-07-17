# Four-Lane Tier-0 Router — Implementation Receipt

Date: 2026-07-17

Review branch: `codex/router-tier0-20260717`

Implementation commit: `b920876`

## Scope

- `[VERIFIED]` The router is a document-level scope prior. It does not emit
  answer evidence, replace chunk evidence, or write corpus data.
- `[VERIFIED]` Both new settings ship default-OFF:
  `FOUR_LANE_TIER0_ROUTER_ENABLED` and
  `FOUR_LANE_TIER0_SUBQUERY_DECOMPOSITION_ENABLED`.
- `[VERIFIED]` The legacy Tier-0 path remains the rollback path. Its Qdrant
  filter now explicitly excludes `chunk_type=semantic_digest`, preserving the
  existing dark status of purchased digest points while the new router is OFF.
- `[VERIFIED]` The optional decomposition path reuses the existing cached,
  lifetime-budgeted grounded planner and appends exactly one fixed
  underlying-crafts bridge probe when enabled.

## Preregistration

- Frozen diagnostic:
  `backend/evals/tier0_bridge_diagnostic_v1.json`
- Diagnostic SHA-256:
  `6c348cbf852a26e483ee810f6d3776ce1425955acc53ec4aede880f76dedc4b8`
- Immutable 15-document selection manifest:
  `backend/evals/runpod_e2e_15doc_selection_v1.json`
- Selection-manifest SHA-256:
  `da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00`
- Preregistration commits: `48cf967`, corrected/bound version `d2c9f77`.
- `[VERIFIED]` Every expected document is in the immutable 15-document
  selection. The only exhaustive camera/lens-only title in that selection is
  forbidden from rank 1.

## Implemented lanes and fusion

| Lane | Durable inputs | Behavior |
|---|---|---|
| Lexical | document titles, summaries, parent headings | document-corpus BM25 |
| Semantic | existing document-summary vectors plus authorized digest vectors | max semantic score per document |
| Child rollup | existing child-vector hits | top-hit aggregation by document; no parent embedding |
| Associative | T9.1 domain resolver/affinity view plus accepted digest ontology | domain, superframe, motif, and latent-concept overlap |

`[VERIFIED]` Fusion uses independently attributable lane scores, fixed
per-lane quotas, threshold-based abstention/spillover, reserved associative
seats, and an effective lexical-score demotion for surface matches whose
stored ontology diverges from an active query ontology. Each selected route
records its seat owner, raw/effective lane scores, associative matches,
demotion status, and fused score.

`[VERIFIED]` Semantic digest vectors and ontology profiles fail closed unless
their current document source version, accepted cache, succeeded job, applied
`projection_outbox.v2` row, reconciled application receipt, point identity,
manifest, payload hash, target collection, and vector name close. A legacy
document with a noncanonical source identity rejects only its digest; it
cannot abort routing for the corpus.

## Static, build, and unit receipts

| Gate | Result | Exit |
|---|---:|---:|
| Focused router/orchestrator/query-plan suite | 57 passed | 0 |
| Adjacent retrieval/registry/schema suite | 138 passed | 0 |
| Python compile of changed modules | clean | 0 |
| Black check of the new router module and its new tests | clean | 0 |
| Docker backend build | image digest `sha256:f6a3bd815b158eb5e0371c37709ddd7f9ed647f1fb0b9d947963aaaf0f3b82d8` | 0 |
| Baked-image focused gate (tests copied in per repo ops law) | 57 passed | 0 |
| `git diff --check` | clean | 0 |

Warnings were limited to existing Pydantic protected-namespace and Qdrant
client compatibility warnings.

## Serialized live measurement

All live work ran under `/tmp/polymath-eval.lock` with
`codex/router-tier0-20260717` as holder. Both arms used the immutable
15-document corpus, the exact `anthropic/minimax-m2.7` query route, serial
concurrency, and a green pre-batch MLX readiness probe.

### Frozen 51-execution OFF/ON

| Metric | OFF | ON | Gate |
|---|---:|---:|---|
| Technical success | 1.000 | 1.000 | green |
| Direct document hit | 1.000 | 1.000 | green |
| Lay-language document hit | 1.000 | 1.000 | green |
| Relationship minimum-distinct target | 0.750 | 0.750 | green |
| Corpus-boundary precision | 1.000 | 1.000 | green |
| Citation-source membership | 1.000 | 1.000 | green |
| Negative refusal | 0.444 | 0.333 | known suite RED; reported, not tuned |

`[VERIFIED]` Both arms completed 51/51 with zero row errors. The OFF and ON
runners each returned true `EXIT=1` because the frozen suite's negative
refusal gate is red. The router-specific direct, lay, and relationship
regression prerequisites remained green, but the ON arm did not improve them.

Durable artifacts:

- OFF: `/data/ingest-files/runpod-job-journals/router-tier0-frozen-off-20260717.json`,
  SHA-256 `fd47898f1d47fa868282fc6ec754735836aa4c31af863a2f81cb5020e5682188`.
- ON: `/data/ingest-files/runpod-job-journals/router-tier0-frozen-on-20260717.json`,
  SHA-256 `a3af80fae4f6353e2c1006cb628182228de77eeac97aca6b74491af9d27ee434`.
- ON used two cost-bounded tranches of 35 and 16 executions. Their
  conservative two-attempt ceilings were `$1.790523` and `$0.8185248`.

### Immutable six-query bridge diagnostic

| Query | OFF | ON | ON top three, in order |
|---|---:|---:|---|
| camera motion/story | fail | fail | Blain Brown; VES Handbook; Grammar of the Edit |
| sequence continuity | pass | pass | Grammar of the Edit; VES Handbook; FACS Manual |
| staging/blocking | pass | pass | Directing; VES Handbook; Force Dynamic Life Drawing |
| visual power | pass | pass | VES Handbook; FACS Manual; Directing |
| character motion | fail | fail | VES Handbook; FACS Manual; Laban Movement Analysis paper |
| storyboard | pass | pass | Directing; VES Handbook; Grammar of the Edit |

`[VERIFIED]` OFF and ON both scored 4/6, and every ordered top-three result was
identical. The ON artifact had complete four-lane attribution for all six
queries. True runner exit was `EXIT=1`.

Per-query attribution below reports the maximum raw contribution observed
across each query's top-three routed documents. It is an attribution summary,
not a cross-family comparison of raw scores.

| Query | Lexical | Semantic | Child rollup | Associative | Divergent demotion observed |
|---|---:|---:|---:|---:|---:|
| camera motion/story | 1.000 | 0.415 | 1.000 | 0.000 | no |
| sequence continuity | 1.000 | 0.426 | 1.000 | 0.000 | no |
| staging/blocking | 1.000 | 0.409 | 1.000 | 0.000 | no |
| visual power | 1.000 | 0.000 | 1.000 | 0.000 | no |
| character motion | 1.000 | 0.463 | 1.000 | 0.000 | no |
| storyboard | 1.000 | 0.351 | 1.000 | 0.000 | no |

Durable artifacts:

- OFF: `/data/ingest-files/runpod-job-journals/router-tier0-bridge-off-20260717.json`,
  SHA-256 `8bea7c72a5977670de7af2f31b9156b43d0ed933110acef737343d31bb7b82ce`.
- ON: `/data/ingest-files/runpod-job-journals/router-tier0-bridge-on-20260717.json`,
  SHA-256 `8f0a48f0ef35869c65e327370d3d9c875d15288e6c68b5366d88f6fe210770dc`.

The first ON attempt was stopped after the first immutable RED was observed;
it had completed query 2 and was inside query 3. Its log is preserved as
`router-tier0-bridge-on-original-partial-20260717.log`. A later senior order
required all six. The complete run preserved the repeated attempts and used
the first observed query 1 and query 2 outcomes for the verdict; their ordered
results were identical in both observations. One harness-only invocation
failed before any query because `PYTHONPATH=/app` was omitted; the corrected
invocation added only that canonical environment contract.

## Root cause and durable boundary

`[VERIFIED]` The target E2E corpus has 15 active documents, zero succeeded
semantic-digest jobs, and zero authorized semantic-digest outbox projections.
The Mark corpus has 103 documents, 250 succeeded digest jobs, and 249 applied,
reconciled `projection_outbox.v2` rows. Exact cross-corpus intersections are:

| Durable identity field | E2E ↔ Mark matches |
|---|---:|
| `doc_id` | 0 |
| `source_key` | 0 |
| `source_identity.content_sha256` | 0 |
| normalized original filename | 0 |

`[VERIFIED]` Each authorized Mark digest receipt binds the Mark corpus,
document, parent, parent-text hash, source version, ownership job, point, and
projected payload hash. Reassigning those signals to an E2E book would break
the corpus/source-version closure. Therefore the associative lane was
correctly unavailable for this diagnostic; cross-corpus reuse is not a lawful
fix.

`[VERIFIED]` With associative evidence unavailable, existing high-confidence
grounded routing hints remained at scores `0.98`/`0.95`/`0.905` and were
merged ahead of the router's fused scores. The camera-title hint therefore
kept the forbidden Blain Brown book at rank 1, while the character-motion
query kept Directing below the top three. No title/query exception, threshold
tuning, cross-corpus reassignment, or scorer change was made.

## Corpus integrity and rollback

`[VERIFIED]` The read-only corpus fingerprint changed only in
`ingest_scheduler_state`, a background scheduler heartbeat:

- before stores SHA-256:
  `eb116e096258c5b8e510fbc81b306b7160b1e8b370ec8f773f1cf7aac40861a9`;
- final stores SHA-256:
  `addded761b0cc093999221d22e17313cedffa0c58198b48be8f9fd3ff25062a6`.

All document, chunk, summary, lexicon, extraction, Qdrant, and Neo4j counts
and hashes were identical. No corpus data was written by this work.

The canonical backend was restored healthy from
`/Users/king/polymath_v3.3` with the five canonical compose overlays and image
`sha256:bf79df8914b73fe50c3c52d2d8cccbbf9870167c42502009262b355218c385a3`.
Neither router flag is present in the canonical runtime environment, which is
equivalent to the committed default-OFF contract.

## Verdict

`[VERIFIED]` **REJECTED / NOT PROMOTION-READY.** The implementation remains
available only on the review branch with both flags default-OFF. Static gates
and the frozen direct/lay/relationship characterization are green, but the
immutable bridge acceptance stayed 4/6 instead of 6/6 and the associative
lane has no lawful identity-bound data in the target corpus.

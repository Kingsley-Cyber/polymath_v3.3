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

## Pending serialized measurement

`[VERIFIED]` No live deploy or A/B has been attempted from this branch.
Measurement is serialized behind the shared eval lock and the senior-ordered
temporal then claim-join runs. Required remaining gates are:

1. frozen three-tier OFF/ON with direct and lay no-regression and relationship
   remaining green;
2. the immutable six-query bridge diagnostic;
3. per-query, per-lane attribution and latency;
4. runtime rollback with both flags restored OFF.

`[INFERRED]` The implementation is build-ready, but it is not
promotion-ready until those live gates close.

# Temporal Query Routing A/B Receipt — 2026-07-16

Status: **RED for final acceptance; review-ready implementation.** The immutable
24-query temporal diagnostic met its targets with the feature enabled, but the
required frozen-suite no-regression comparison could not be completed under a
stable embedding contract. Two frozen runs were invalidated by evidenced MLX
embedding transport outages. No selective retry was used.

## Change contract

- Feature flag: `TEMPORAL_QUERY_ROUTING_ENABLED`, default `false`.
- Version: `temporal_query_routing.v1`.
- Scope: query-side temporal intent, bounded Mongo temporal hydration, and
  deterministic temporal/graph evidence preference in the retriever and chat
  retrieval seams only.
- The detector reuses the server extraction runtime's qualified temporal regex
  families and pinned `en_core_web_sm==3.8.0` DATE/TIME/EVENT fallback.
- Exact or boundary-contained temporal surfaces are required for temporal
  admission. Temporal class and role are refinement/tie-break signals only.
- The feature fails open, never filters an otherwise non-empty result set to
  empty, preserves QueryPlanV2 evidence reservations, and performs no writes.

## Immutable inputs

| Artifact | SHA-256 |
|---|---|
| Frozen 17-query preregistration | `8f70b1d375120862712fa4a44abad5ca7eb38eb0fbc7d3a3a86e79f4827bc110` |
| Temporal 8-query/24-execution preregistration | `9dcb147ccbfe54779e87307d2826d4565da4c43608354abf889a4ca701eef5d1` |
| 15-document selection | `da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00` |
| Frozen runner | `b9a1aa940b589d8abff3d1875bb6b362e1e05248116f7fdcc1b2abb487f5e347` |
| Corpus | `2c894530-8d57-4432-a6d4-bc14505a698b` |

All eval arms used concurrency `3` and the same corpus, model configuration,
runner, preregistration, selection, and request shape. The paired rerun held
the reranker-unavailable fallback state constant by senior ruling.

## Temporal diagnostic result

The same-build OFF control was already substantially above the older diagnostic
baseline. The feature's attributable improvement is therefore the
same-build OFF-to-ON delta, not the historical-to-ON delta.

| Metric | Historical baseline | Same-build OFF | ON | Target |
|---|---:|---:|---:|---:|
| Overall document hit | 0.7917 | 0.9583 | **0.9583** | >=0.90 |
| Overall full-anchor coverage | 0.4583 | 0.7500 | **0.8750** | >=0.70 |
| Qdrant-only document hit | 0.3750 | 0.8750 | **0.8750** | report |
| Qdrant-only full-anchor | 0.0000 | 0.6250 | **0.7500** | report |
| Qdrant+Mongo document hit | 1.0000 | 1.0000 | **1.0000** | report |
| Qdrant+Mongo full-anchor | 0.6250 | 0.7500 | **0.8750** | report |
| Graph document hit | 1.0000 | 1.0000 | **1.0000** | report |
| Graph full-anchor | 0.7500 | 0.8750 | **1.0000** | report |
| Technical success | 1.0000 | 1.0000 | **1.0000** | 1.00 |
| Corpus boundary precision | 1.0000 | 1.0000 | **1.0000** | 1.00 |

Same-build ON improved full-anchor completion by `+0.125` overall: one added
complete anchor in each tier, with no document-hit loss.

### Per-query tier receipt

Each cell is `document_hit/full_anchor_complete`.

| Query | Tier | OFF | ON |
|---|---|---:|---:|
| `temporal_1929_dialog` | `qdrant_only` | 1/1 | 1/1 |
| `temporal_1929_dialog` | `qdrant_mongo` | 1/1 | 1/1 |
| `temporal_1929_dialog` | `qdrant_mongo_graph` | 1/1 | 1/1 |
| `temporal_editing_1927` | `qdrant_only` | 1/1 | 1/1 |
| `temporal_editing_1927` | `qdrant_mongo` | 1/1 | 1/1 |
| `temporal_editing_1927` | `qdrant_mongo_graph` | 1/1 | 1/1 |
| `temporal_editing_century` | `qdrant_only` | 0/0 | 0/0 |
| `temporal_editing_century` | `qdrant_mongo` | 1/1 | 1/1 |
| `temporal_editing_century` | `qdrant_mongo_graph` | 1/1 | 1/1 |
| `temporal_facs_1980s_2002` | `qdrant_only` | 1/0 | **1/1** |
| `temporal_facs_1980s_2002` | `qdrant_mongo` | 1/1 | 1/1 |
| `temporal_facs_1980s_2002` | `qdrant_mongo_graph` | 1/1 | 1/1 |
| `temporal_murch_origin_1988_2001` | `qdrant_only` | 1/0 | 1/0 |
| `temporal_murch_origin_1988_2001` | `qdrant_mongo` | 1/0 | 1/0 |
| `temporal_murch_origin_1988_2001` | `qdrant_mongo_graph` | 1/1 | 1/1 |
| `temporal_nicole_2006` | `qdrant_only` | 1/1 | 1/1 |
| `temporal_nicole_2006` | `qdrant_mongo` | 1/1 | 1/1 |
| `temporal_nicole_2006` | `qdrant_mongo_graph` | 1/1 | 1/1 |
| `temporal_noir_1940s_1950s` | `qdrant_only` | 1/1 | 1/1 |
| `temporal_noir_1940s_1950s` | `qdrant_mongo` | 1/1 | 1/1 |
| `temporal_noir_1940s_1950s` | `qdrant_mongo_graph` | 1/1 | 1/1 |
| `temporal_vfx_2012_2013` | `qdrant_only` | 1/1 | 1/1 |
| `temporal_vfx_2012_2013` | `qdrant_mongo` | 1/0 | **1/1** |
| `temporal_vfx_2012_2013` | `qdrant_mongo_graph` | 1/0 | **1/1** |

## Frozen-suite no-regression gate

| Arm | Direct | Lay | Relationship | Technical | Result |
|---|---:|---:|---:|---:|---|
| Same-build OFF | 1.0000 | 1.0000 | 0.7500 | 1.0000 | Control completed; process EXIT=1 only because the pre-existing negative-refusal gate was 0.4444. |
| First ON | 0.7778 | 0.9167 | 0.7500 | 1.0000 | **Invalid operational RED.** Four Qdrant-only direct arms had zero candidates during an MLX embedding outage. |
| Authorized paired OFF rerun | 1.0000 | 1.0000 | 0.7500 | 1.0000 | **Invalid operational RED.** A new embedding timeout occurred late in the arm; the paired ON arm was not started. |

The first ON misses were exactly:

- `direct_rule_of_six::qdrant_only`
- `direct_laban_machine::qdrant_only`
- `direct_edit_grammar::qdrant_only`
- `direct_anatomy_masses::qdrant_only`

For all four, the deployed detector returned `active=false`, zero temporal
expressions, and no detector error. Their retrieval traces reported Qdrant
`summaries=0`, `children=0`, `merged=0`, and failures containing
`embedding:TimeoutError` plus `corpus_vocabulary:TimeoutError`. Backend logs
also recorded embedder disconnects/all-connection-attempts-failed. Their Mongo
and graph arms remained green through lexical hydration. Compose backend
configuration hashes were identical after removing the single feature flag.

The senior authorized one complete paired OFF-to-ON rerun after a health gate
of three identical 1024-dimensional embeddings, backend health, and a clean
timeout log tail. OFF passed that preflight, but a new
`embedding:TimeoutError` appeared at `2026-07-17T03:36:44Z`. Per the ruling,
the run stopped, no paired ON arm ran, and no third attempt is allowed.

Therefore **the direct >=0.85 and lay >=0.75 no-regression acceptance remains
unverified**, even though the invalidated paired OFF journal eventually closed
with direct and lay both at 1.00.

## Tests and operational receipts

| Check | Actual result |
|---|---|
| Initial canonical-container pytest path | EXIT=4 because tests are not baked into the image. |
| Corrected focused pytest after `docker cp` of the exact test files | **39 passed**, EXIT=0. |
| New temporal test file | 22 assertions passed as part of the focused set. |
| Adjacent retrieval tests | `test_rerank_text_hydration.py`, `test_funnel_a_fair_mode.py`, `test_relationship_evidence_feature_flag.py`, and `test_planned_retrieval.py` passed. |
| Pinned spaCy detector smoke | All 8 temporal preregistration query shapes detected. |
| Ruff | New helper/tests passed; two existing `chat_orchestrator.py` F401 findings were pre-existing and untouched. |
| First worktree deploy | EXIT=1 before mutation because the worktree-local override was absent. Corrected with the canonical absolute override; backend-only deploy EXIT=0. |
| Paired OFF switch | EXIT=1 before mutation because the canonical env file was omitted and runtime binds expanded to Windows defaults. Corrected with canonical absolute `--env-file`; backend-only recreate EXIT=0. No env value was printed, copied, or committed. |
| Canonical restore | `/Users/king/polymath_v3.3` at `3157ec9`, backend-only build/recreate EXIT=0; healthy; live `chat_orchestrator.py` SHA matched canonical; temporal module/setting absent. |

## Corpus write audit

- **VERIFIED:** The feature implementation contains no Mongo, Qdrant, or
  Neo4j write operation.
- **VERIFIED:** Post-run Qdrant point counts/payload hashes, Neo4j node and
  relationship hashes, and substantive Mongo hashes matched an independent
  before/after fingerprint pair for the same corpus. Removing only the
  heartbeat-bearing `mongo.ingest_scheduler_state` row produced the identical
  substantive stores hash in all three artifacts:
  `82580d560f5ae341841052b9972587764ed4d3c6809792216be0b523f8f1bdcb`.
- **VERIFIED:** The full post-run stores hash was
  `c687d3538dc91ab4e0f25ea2873830c7835eea3d7085894edbeddeaa66114e29`.
- **INFERRED:** Full-store hash drift from the executor's preflight hash
  (`5119500495c93c139650e2b501a6278989dc0182efb1090b5afa0748c414bf35`)
  was scheduler-heartbeat churn. The executor's complete preflight JSON was
  lost when the temporary backend container was recreated, so that exact
  per-collection comparison cannot be repeated. The independent before/after
  artifacts show `ingest_scheduler_state` as the only differing collection.
- **ASSUMED:** None for the metric or test claims above.

## Artifact hashes

| Artifact | SHA-256 |
|---|---|
| Same-build OFF temporal JSON | `74ed37951a6396490633df8c7c03eb05cb04937aa6beace53befb9a330068caa` |
| ON temporal JSON | `061156a9ce0f4218901304a26f4ee0e02bcd861e916bf507b05e653d561f2bba` |
| Same-build OFF frozen JSON | `c0b5cf6b6514f2754e1a703ba535da9045e54f9e05ab7b3f520fcc51bf166322` |
| Invalid first ON frozen JSON | `2737ca35f230989b5b115459b02efee5894ff9d0da74430aa61f2b225c14b8c0` |
| Invalid paired OFF journal | `568e8309028b58d81f222aaafcdb1f2b56d083a529d631686399bcaa28a9d9f0` |
| Invalid paired OFF log | `72aca37f8968462d52ac7b83a09331d931a19b201b985e16161cdde99f36976f` |
| Post-run fingerprint JSON | `44e224366b5435e5f9013ee6f04771641afdb5c6a545f10cf58f559771b8a34a` |

Final disposition: keep the flag default OFF. Senior review may merge the
dark-shipped implementation, but activation requires a future full paired
frozen A/B under a stable embedding service; this receipt does not authorize
activation.

# Codebase Intent Gap Analysis

**Verdict:** PASS
**Repository:** /Users/king/polymath_v3.3
**Plan:** POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK/PROMPT_TO_AGENT.md plus frozen technical-book scoring policy
**Revision:** 86149227db703c097aad586e4a18018185e9f742 with preserved working-tree changes
**Audited at:** 2026-08-07T03:05:33Z

The highest-impact remaining boundary is not an implementation gap: production graph-write promotion is still unauthorized. All results below come from isolated namespaces. The production boundary in `ACCEPTANCE_CRITERIA.md` explicitly keeps that release decision separate from E2E and held-out qualification.

## Intent Contract

The requested outcome is a CPU-only Graphify path using one warm `fastino/gliner2-base-v1` provider and triplet-extract Balanced CPU. It must preserve the canonical ingestion entrypoint; persist raw, reduced, and assertion artifacts before projection; rebuild Neo4j from canonical artifacts; remain idempotent; pass entity, relation, speed, and reachability gates; and score the frozen technical-book answer key without embedding or unconstrained fuzzy matching.

The scoring contract freezes the 10,366-byte Markdown fixture, 66 positive assertions, 5 qualified assertions, namespace, matching registries, and scorer release. Alias normalization and declared subsumption remain distinct match operations. Every no-match row must have one of the ten declared failure-taxonomy classes and three recall checkpoints.

Non-goals are retrieval redesign, Query IR changes, database replacement, GPU or Apple accelerator work, model fine-tuning, another semantic model, ontology replacement, and production graph promotion.

## Actual Runtime

The critical path is:

`IngestionService.ingest` -> `backend/services/ingestion/worker.py` -> `run_graphify_pipeline` -> normalization -> survey -> CPU entity census -> document reducer -> mention completion -> relation eligibility -> triplet-extract OpenIE -> argument adaptation -> proposition reduction -> predicate compilation -> assertion assembly -> relation validation -> `ghost_b_extractions` and stage artifacts -> `_write_neo4j_for_doc`.

The two authoritative Markdown fixtures ran through that path in namespace `openie_final_v12`. The measured projection contains 425 nodes, 434 mentions, 15 relations, 175 assertion edges, and 175 assertion-support edges. Delete-and-rebuild and an independent repeat produced digest `130a8300be27a5c2dfe4de437efbfdc796d81d7d5704de4870aee3b48e40665f` with identical counts.

The technical-book test ran separately in namespace `technical_book_stress_v1` against the frozen answer-key bytes. Scorer `stress-answer-key-scorer-v4-qualified-taxonomy` matched 61 of 66 gold assertions at 0.9242 precision and recall, leaked zero qualified assertions into positive edges, used no prohibited matchers, and promoted 66 ontology-scoped edges plus 2 explicitly reported `stores` edges.

## Gap Matrix

| ID | Requirement | Expected evidence | Observed evidence | Status | Impact | Dependency | Smallest remediation | Verifier |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| REQ-ENTRY | Canonical ingestion path executes the new pipeline | Import and awaited call from the production worker | entrypoint: `IngestionService.ingest`; wiring: `worker.py` imports and awaits `run_graphify_pipeline`; outcome: two fixtures persisted and projected; verification:PASS `e2e_proof_final.json` | WORKING | Required for production reachability | Existing ingestion worker | Keep the worker import and awaited call covered by reachability validation | `controller.py run-stage FINAL_VERIFY` |
| REQ-CPU | One warm GLiNER2 model runs on CPU only | Provider release pins, one load, no retired or accelerator provider | entrypoint: `get_gliner2_cpu_provider`; wiring: census receives the shared provider; outcome: `model_load_count=1`, retired runtime models 0, CPU check true; verification:PASS `e2e_quality_final.json` | WORKING | Prevents memory duplication and forbidden runtimes | GLiNER2 checkpoint pin | Retain CPU assertions and singleton registry checks | `verify_gliner2_cpu_provider.py` plus E2E quality check |
| REQ-STAGES | All deterministic extraction stages execute in order | Reachable calls and persisted stage receipts | entrypoint: `run_graphify_pipeline`; wiring: census, reducer, completion, OpenIE, adapter, proposition reducer, compiler, assembler, and validation are called; outcome: 16 stage artifacts per fixture; verification:PASS E2E reports | WORKING | Establishes full semantic path | CPU provider and triplet-extract | Keep the release-pinned stage ledger and conservation checks | `run_graphify_fixture_e2e.py` |
| REQ-PERSIST | Mongo is canonical and OpenIE does not write directly to graph | Durable extraction artifacts before projection | entrypoint: worker persistence branch; wiring: rows stored in `ghost_b_extractions` and stage collection before `_write_neo4j_for_doc`; outcome: 226 ghost rows across E2E fixtures; verification:PASS rebuild test | WORKING | Preserves rebuildability and audit trail | Pipeline output contracts | Keep canonical-artifact persistence ahead of projection | `graph_rebuild_repeat_final.json` |
| REQ-REBUILD | Neo4j rebuilds exactly from canonical artifacts | Delete, rebuild, and graph snapshot equality | entrypoint: isolated rebuild runner; wiring: Mongo artifacts feed the Neo4j writer; outcome: identical digest and five identical count classes; verification:PASS `graph_rebuild.json` | WORKING | Prevents graph-as-source-of-truth drift | Canonical persistence | Retain isolated deletion and digest comparison | `controller.py run-stage REBUILD_GRAPH_FROM_CANONICAL_ARTIFACTS` |
| REQ-IDEMP | Repeated execution creates no duplicates | Two-run identity and count equality | entrypoint: canonical E2E runner; wiring: deterministic IDs and upserts; outcome: first and second snapshots identical with zero count delta; verification:PASS `idempotency_final.json` | WORKING | Required for safe retries | Stable IDs and canonical store | Keep the repeat-run verifier in the release gate | `controller.py run-stage RUN_IDEMPOTENCY_TEST` |
| REQ-QUALITY | Entity and relation acceptance thresholds pass | Measured fixture metrics at or above every threshold | entrypoint: canonical quality fixture; wiring: scorer consumes persisted pipeline artifacts; outcome: entity F1 0.8707, relation F1 0.9286, precision 1.0, qualification accuracy 1.0; verification:PASS `e2e_quality_final.json` | WORKING | Protects graph precision and recall | Frozen quality fixture | Re-run fixture after narrow semantic changes | `score_quality.py` through E2E runner |
| REQ-SPEED | Full refactored fixture is at least 1.5 times baseline speed | Frozen baseline and candidate wall-clock totals | entrypoint: canonical throughput fixture; wiring: full ingest timings compared to Relex baseline; outcome: 11.9837 seconds versus 26.7829 seconds, ratio 2.2349; verification:PASS baseline comparison | WORKING | Satisfies performance gate without semantic-model expansion | Frozen Relex baseline | Retain full-path timing comparison | `controller.py run-stage COMPARE_TO_BASELINE` |
| REQ-SCORE | Frozen answer key uses five explicit match classes and no fuzzy scorer | Frozen policy hash, class counts, prohibited matcher list | entrypoint: `run_graphify_stress_answer_key.py`; wiring: v4 scorer loads separate alias and subsumption registries; outcome: 61 normalized variants and 5 no-match rows, no prohibited matcher; verification:PASS `score_v4.json` and `semantic_safety.json` | WORKING | Makes the quality claim reproducible | Frozen fixture, key, namespace, policy, scorer | Preserve freeze hashes and rerun all 66 after each correction | `audit_graphify_stress_frozen.py` and strict semantic verifier |
| REQ-TAXONOMY | Every unmatched gold row has a failure class and three recall checkpoints | 5 classified rows and aggregate checkpoint counts | entrypoint: v4 scorer; wiring: no-match rows flow to deterministic taxonomy; outcome: 2 missing-pair, 2 entity-type, 1 gate-policy; all direction, polarity, modality, attribution, conditional-scope, argument-alignment, and predicate-mapping counts are zero; verification:PASS `failure_taxonomy_v4.json` | WORKING | Locates the real recall loss | Frozen scoring policy | Use the taxonomy before any later semantic correction | Validate no-match count equals taxonomy row count |

## Directory Contract

`backend/services/extraction/` owns the extraction stages and their deterministic semantic policies. `backend/models/graphify_contracts.py` owns serialized contracts. `backend/services/ingestion/worker.py` owns canonical orchestration, durable extraction persistence, and the call into graph projection. `backend/services/graph/neo4j_writer.py` owns projection semantics. `backend/scripts/` owns executable fixture and frozen-answer-key harnesses. `backend/tests/extraction/` and focused graph tests own unit and integration checks. `POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK/work/` owns generated receipts, frozen test snapshots, metrics, and reports.

The boundary is coherent: extraction modules do not open graph sessions, and the graph writer does not become a source of extraction truth. The vendored triplet-extract source under the pack is an exact runtime dependency snapshot, not a second repository-owned extraction implementation.

## Remediation Order

No implementation remediation is required for the scoped acceptance contract. Any later quality work should proceed only in this order:

1. Select one remaining failure class from the frozen taxonomy.
2. Make one narrow deterministic semantic correction without changing frozen bytes, namespaces, registries, or scorer behavior.
3. Rerun the exact 66 assertions and compare all three recall checkpoints.
4. Re-run focused unit tests, two-document E2E, rebuild, and idempotency if the correction touches shared runtime logic.
5. Keep production promotion pending until its separate authorization and license review exist.

## Verification Record

- Focused Graphify extraction and vendored attribution/negation suites: 76 passed.
- Two-document canonical E2E: passed, one warm model, no retired runtime provider, graph rebuild matched.
- Entity fixture: exact-span and type F1 0.8707483, strict alignment 1.0, accepted pronoun endpoints 0.
- Relation fixture: precision 1.0, recall 0.8666667, F1 0.9285714, qualification accuracy 1.0, forced fallbacks 0.
- Throughput fixture: 81,822 bytes in 11.9837218 seconds; Relex baseline 26.7829212 seconds; ratio 2.2349.
- Independent graph rebuild: passed with identical digest and counts.
- Idempotency: passed with identical first and second snapshots.
- Frozen technical-book score: passed at 61/66 with 0.9242 precision/recall/F1, zero prohibited matchers, zero qualified-to-positive leakage, and strict semantic-safety verification passed.

## Residual Unknowns

- Production graph-write promotion is pending by policy and was not exercised.
- The vendored triplet-extract dependency declares GPL-3.0-or-later while the repository declares MIT. Distribution and deployment implications require a license review.
- Five gold assertions remain unmatched. They are exposed in the taxonomy rather than treated as semantic matches.

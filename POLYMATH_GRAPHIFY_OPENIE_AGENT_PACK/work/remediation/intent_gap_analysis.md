# Codebase Intent Gap Analysis

**Verdict:** FAIL
**Repository:** `/Users/king/polymath_v3.3`
**Plan:** Autonomous Graphify semantic remediation and release verification
**Revision:** `86149227db703c097aad586e4a18018185e9f742` plus preserved dirty worktree
**Audited at:** 2026-08-06T22:45:00-06:00

## Intent Contract

## Global objective

Make the existing Graphify GLiNER2 CPU and OpenIE pipeline pass the frozen exposed development regressions, assertion-containment invariants, canonical two-document E2E, graph rebuild, idempotency, provider reachability, and then performance. Do not use these exposed fixtures as evidence of generalization.

## Required architecture

`GLiNER2 CPU -> document entity reduction -> mention completion -> triplet-extract/OpenIE -> OpenIE argument alignment -> proposition reduction -> deterministic predicate compiler -> clause-local assertion semantics -> evidence gate -> canonical storage -> Neo4j projection`

## Boundaries

- No new neural model.
- No Graphify redesign.
- No speed optimization before semantic quality passes and its digest is frozen.
- No answer-key, alias, subsumption, scorer, namespace, or threshold changes made in response to extraction misses.
- No fixture-specific production rule. Each correction must own a demonstrated general failure class and include unseen paraphrase and negative regressions.
- Qualified assertions remain canonical evidence records but do not count as positive Neo4j facts.

## Release outputs

- Frozen evaluation manifest and pre-run verifier.
- First-loss-stage ledger for every unmatched gold assertion.
- Full metrics for entity, pair, surface proposition, canonical predicate, positive assertion, qualified assertion, and containment checkpoints.
- Equal graph rebuild digests and equal second-run digests.
- Reachability evidence from the canonical worker entrypoint.
- Passing performance result after semantic parity.

## Actual Runtime

## Production path

1. `backend/services/ingestion/worker.py` imports and awaits `run_graphify_pipeline`.
2. `backend/services/extraction/graphify_pipeline.py` executes census, reduction, mention completion, eligibility, OpenIE, argument adaptation, proposition reduction, predicate compilation, assertion assembly, syntax union, validation, and emits canonical extraction results.
3. Canonical artifacts are persisted in MongoDB.
4. `backend/services/graph/neo4j_writer.py` projects accepted results to Neo4j.
5. Qdrant participates in the canonical ingest path and must be reachable for the requested two-document E2E.

## Evaluation paths found

- The technical-book scorer reads frozen Mongo stage payloads and Neo4j projections. Its v4 scorer and v2 matching policy are hash-locked.
- The Meridian first-run harness directly invokes the pipeline and Neo4j writer, but bypasses Qdrant and is not a canonical E2E.
- The controller's final verifier reads a hardcoded `correction_16` result and incorrectly maps that exposed development result to held-out qualification.

## Gap Matrix

| ID | Requirement | Expected evidence | Observed evidence | Status | Impact | Dependency | Smallest remediation | Verifier |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| REQ-001 | Frozen input bytes | All fixture, key, policy, and scorer hashes equal the manifest | entrypoint: `verify_freeze.py`; wiring: manifest paths to eight components; outcome: all hashes equal; verification:PASS | WORKING | Prevents score improvement through evaluation drift | None | Keep the verifier mandatory before and after every run | `python3 .../freeze_v1/verify_freeze.py` |
| REQ-002 | Canonical runtime reachability | Runtime receipts prove worker, pipeline, stores, and projection executed | Worker imports and awaits Graphify, but the old gate inspects source text only | PARTIAL | A passing direct harness may not prove production wiring | Healthy providers | Execute canonical ingest and assert persisted stage receipts | Canonical worker E2E receipt checker |
| REQ-003 | Technical-book semantics | Current code passes the frozen 66-positive and 5-qualified policy | Existing correction 16 reports 61/66 after 16 exposed correction cycles | PARTIAL | Result is stale and development-only | Frozen scorer and live stores | Rerun current code through declared namespace without policy changes | Frozen v4 scorer plus checkpoint report |
| REQ-004 | Meridian semantics | Current code meets all requested entity, pair, predicate, triple, and trap gates | First direct run reports entity P 0.631, R 0.911, strict F1 0.417, and 2 trap leaks | PARTIAL | Semantic quality and containment fail | Full stage ledger | Build first-loss report, then fix the largest general causal class | Canonical five-class and first-loss scorer |
| REQ-005 | Evaluation claim integrity | Final gate resolves immutable artifacts and labels exposed tests as development | `stages/final_verify.py` hardcodes `correction_16` and reports held-out pass | PARTIAL | Release claim is false even when numbers are accurate | New release evidence layout | Resolve the freeze manifest and report qualification pending | Final verifier unit and provenance tests |
| REQ-006 | Canonical two-document E2E | Both frozen Markdown files traverse worker to canonical stores and Neo4j | Prior pack receipts cover different synthetic fixtures | PARTIAL | Integration behavior is unproven for requested documents | Qdrant health and semantic pass | Run both frozen files through the worker in isolated namespaces | Stage receipt and projection assertions |
| REQ-007 | Graph rebuild and idempotency | Rebuild digest equals source digest and run two adds no duplicates | Prior synthetic receipts pass, but post-remediation artifacts are untested | PARTIAL | Projection may diverge after semantic fixes | Semantic digest freeze | Rebuild and repeat ingest against the final canonical assertions | Digest and duplicate-count commands |
| REQ-008 | Provider reachability | Qdrant, MongoDB, Neo4j, and GLiNER2 CPU are healthy and executed | Qdrant is still recovering after exact corrupt derived collections were quarantined | BLOCKED | Canonical ingest cannot start | Qdrant recovery completion | Wait for recovery, then run health and execution probes | Container health plus worker stage receipts |
| REQ-009 | Performance | Passing semantics remain byte-for-byte equal while speed meets the gate | Prior speed receipt predates this quality remediation | PARTIAL | Optimization could silently regress meaning | All semantic gates | Optimize only the measured dominant CPU stage after digest freeze | Semantic parity and speed scorer |
| REQ-010 | General qualification | A sealed, never-inspected set is scored once after code and policy freeze | Both supplied answer keys and item failures are exposed | MISSING | No honest general extraction-quality claim is possible | Separately authored sealed fixture | Keep qualification pending until a sealed evaluation exists | One-shot qualification receipt with exposure provenance |

## Directory Contract

| Directory | Owner responsibility | Allowed remediation |
| --- | --- | --- |
| `backend/services/extraction/` | Linguistic stages and deterministic compilation | Narrow general rule at the first failing owner only |
| `backend/models/graphify_contracts.py` | Cross-stage schemas and terminal lanes | Contract change only when a demonstrated failure cannot use an existing field |
| `backend/services/graph/` | Projection of accepted canonical facts | Promotion and idempotency fixes only, never extraction heuristics |
| `backend/services/ingestion/` | Canonical orchestration and provider reachability | Integration fixes only, no benchmark semantics |
| `backend/tests/extraction/` | Portable invariant regressions | Exact failure class, two unseen paraphrases, one negative or adjacent path |
| `POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK/work/remediation/` | Frozen provenance, cycle receipts, and release evidence | Evaluation artifacts only, never production extraction behavior |

## Remediation Order

1. Restore Qdrant health without deleting source documents.
2. Add a canonical, hash-verifying scorer that reports the required five match classes, first-loss stage, positive and qualified lanes separately, and all hard invariants.
3. Run both frozen development fixtures from current code to establish one comparable baseline.
4. Fix the largest first-loss causal class at its existing owner seam. Add general paraphrase and negative regressions.
5. Verify the local regression, all extraction tests, both frozen fixtures, and freeze hashes. Repeat.
6. Rerun canonical two-document E2E, graph rebuild, idempotency, and provider reachability.
7. Freeze passing semantic graph and assertion digests.
8. Optimize only the measured dominant CPU stage, requiring frozen semantic parity after every change.
9. Replace the hardcoded final verifier inputs with the declared immutable evaluation artifacts and truthful development versus qualification status.

## Verification Record

- `2026-08-06`: fixture, answer-key, policy, and scorer hashes for both exposed regressions verified: PASS.
- `2026-08-06`: Qdrant corruption ownership resolved to derived or isolated test collections; exact directories moved to recoverable quarantine: PASS.
- `2026-08-06`: Qdrant restart progressed beyond all three previous panics: IN PROGRESS.
- Current repository commit: `86149227db703c097aad586e4a18018185e9f742` with a dirty working tree that predates this remediation. Unrelated changes are preserved.

## Residual Unknowns

- The exact first-loss distribution for Meridian has not yet been calculated from its persisted stage ledger.
- Current-code technical-book results have not yet been rerun after the new freeze.
- No sealed one-shot qualification fixture is available. General quality must remain pending even if all exposed development gates pass.

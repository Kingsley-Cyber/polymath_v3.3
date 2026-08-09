# Minimal Query IR / Control-Plane Integration Plan

**Audit date:** 2026-08-02 · Companion to `QUERY_CONTROL_PLANE_REPO_AUDIT.md`
Stages 6 (minimal integration design) + 7 (dependency-ordered implementation).

**Owner decision (2026-08-02, supersedes earlier framing):**
- Query IR EXTENDS `QueryPlanV2` — no second planner, no parallel query
  architecture. Seam: `POST /api/chat → process_chat_request →
  build_query_plan_v2 → QueryPlanV2 + typed IR fields → QueryIR validation →
  retrieve_planned`.
- Slice 1 is "typed contract and release-metadata scaffolding" — NOT a
  complete release gate. Active release registry, promotion history, and
  graph-write enforcement belong to the later control-plane slice.
- Readiness gating is **route- and artifact-aware**, never a blanket hard
  gate and never purely advisory. See `ReadinessDecision` below.
- Canonical graph writes stay disabled until the FULL categorical release
  chain passes (`graph_write_allowed` predicate below).

---

## Route-Aware Readiness Contract (owner-mandated)

`query_ready` is NOT one universal boolean. Different routes require
different artifacts; a corpus may legitimately serve entity lookup before it
can serve canonical graph traversal.

```python
class ReadinessDecision:
    corpus_id: str
    route: str
    allowed: bool
    mode: Literal["full", "partial", "blocked"]
    required_artifacts: tuple[str, ...]
    missing_artifacts: tuple[str, ...]
    certificate_id: str | None
    release_pins: dict[str, str]
    reasons: tuple[str, ...]
```

### Required route gates

| Operation | Required readiness |
|---|---|
| Ingest status and artifact inspection | Always allowed |
| Per-chunk extraction inspection | Extraction artifact exists |
| Entity/vocabulary lookup | `entity_lexicon` ready |
| Vector or hybrid search | Child vectors + required parent/summary records |
| Curated chat synthesis | Required retrieval artifacts + minimum evidence obligations |
| Graph reads | Qualified graph projection matching requested release pins |
| Canonical graph writes | Full extraction qualification + graph-write promotion |
| Ontology/administrative writes | Approval + idempotency key + authorization + active release |

### Canonical graph-write predicate (deny-by-default)

`graph_promotion_jobs.py` denies writes unless the active release bundle
proves EVERY mandatory state:

```python
def graph_write_allowed(release: ReleasePin) -> bool:
    return all(
        (
            release.engineering_freeze == "passed",
            release.recoverability == "passed",
            release.git_reproducibility == "passed",
            release.closed_world_annotation == "passed",
            release.calibration == "passed",
            release.held_out_qualification == "passed",
            release.graph_write_promotion == "passed",
            release.extractor_hash_matches,
            release.ontology_hash_matches,
            release.acceptance_policy_hash_matches,
            release.schema_hash_matches,
        )
    )
```

Failure leaves the durable job intact:

```json
{
  "state": "blocked_no_release",
  "retryable": true,
  "missing_conditions": ["held_out_qualification", "graph_write_promotion"]
}
```

Percentage estimates are never promotion criteria — categorical state only.

### Shadow graph-read rule

`NEO4J_ENABLED=true` alone is insufficient. Every graph result must carry:
`corpus_id, extractor_release, ontology_release, acceptance_policy_release,
promotion_version, assertion_lane, canonical_or_shadow, certificate_id`.
Enforcement: canonical factual synthesis → only matching promoted release
pins; experimental inspection → shadow allowed but explicitly labeled;
release mismatch → excluded or returned as non-authoritative diagnostic
evidence. This prevents legacy/unqualified edges from contaminating answers
while writes are disabled.

---

## Correct implementation order (owner-mandated, supersedes slice order)

```
1.  Complete the Git-frozen extraction commit and tag
2.  Implement Slice 1 contracts and adapters
3.  Stamp release metadata on extraction, promotion and query traces
4.  Add deny-by-default graph-promotion release check
5.  Extend QueryPlanV2 into validated QueryIR
6.  Run the Query IR gate in warn mode
7.  Add route-aware ReadinessDecision at chat and MCP seams
8.  Add ontology resolution after vocabulary resolution
9.  Normalize hydrated results into EvidenceItem
10. Add contradiction and post-synthesis claim verification
11. Benchmark FAST, CURATED and RESEARCH routes
12. Enforce Query IR and readiness gates after regression and soak tests
13. Promote graph writes only after held-out qualification passes
```

The route benchmark must run before any latency claim or finalized cap; the
limits in `QUERY_PERFORMANCE_BENCHMARK_PLAN.md` are limits TO TEST, not
measured performance.

---

## Step 2 (Slice 1) — closeout state (owner-accepted)

Slice 1 = typed contracts + adapters + release metadata scaffolding +
additive diagnostics − runtime enforcement − graph promotion − query gating.

```yaml
slice_1_contracts: passed
query_ir_contract: passed
release_state_contract: passed
vocabulary_wrapper_contract: passed
evidence_item_contract: passed
frozen_extraction_regression: passed   # 451 passed
runtime_behavior_change: none
query_ir_enforcement: not_started
graph_write_enforcement: not_started
```

Files: `models/query_ir.py`, `models/release_state.py`,
`models/vocabulary_resolution.py`, `models/evidence_item.py`, additive
`release_pins` diagnostics key in `services/retriever/__init__.py`, four
contract suites in `backend/tests/`.

---

## Step 3 — release-identity stamps (owner acceptance contract)

Stamp release identity on existing artifacts WITHOUT using it to permit or
deny behavior. The categorical release state remains the normative
authority; percentage estimates never enter ReleasePin, readiness,
certificate, or promotion decisions.

Required stamp shape (11 nullable pins):

```json
{
  "extractor_release": null,
  "extractor_commit_sha": null,
  "extractor_config_hash": null,
  "ontology_release": null,
  "ontology_hash": null,
  "acceptance_policy_release": null,
  "acceptance_policy_hash": null,
  "schema_release": null,
  "schema_hash": null,
  "promotion_release": null,
  "certificate_id": null
}
```

Stamp rules: immutable per artifact; JSON-serializable; nullable when
historical data lacks a value; copied through adapters without
reinterpretation; included in deterministic hashes only where explicitly
intended; non-authoritative during this step.

Attach points (implemented):

- Extraction → `_stamp_extraction_row_identity` in
  `services/ingestion/extraction_jobs.py` (stamps every persisted
  ghost_b_extractions row: ok/error/skipped) + optional `release_stamp`
  field on `ChunkExtraction` (`models/contracts.py`).
- Promotion → `promote()` delta in `services/ingestion/promote.py` copies
  the extraction stamp verbatim onto Qdrant payloads and the Mongo chunks
  mirror; graph-promotion job attempts carry the stamp via
  `classify_graph_promotion_candidate` (claim leg stays null until claims
  carry stamps).
- Query → `QueryIR.release_stamp` (`models/query_ir.py`) +
  `release_stamps_read` diagnostics key in `retrieve_planned` (verbatim
  collection from hydrated chunk metadata; conflicts stay visible). MCP and
  run traces inherit it through the existing diagnostics plumbing.

Contract module: `models/release_stamp.py` (`ReleaseStamp`,
`current_release_stamp()` — all-null until the active release registry
exists, `copy_stamp`, `stamp_conflicts`, `observed_release_stamps`).

Acceptance results (owner-corrected closeout, 2026-07-27):

```yaml
step_3_implementation: passed
release_stamp_contracts: passed
backward_compatibility_contracts: passed
frozen_extraction_regression: passed    # 451 passed
runtime_authority: disabled
docker_promotion_integration: passed    # 2026-08-03, gates below
async_job_regression: passed            # 116 passed in-container
```

Reporting corrections (owner-mandated):

- Requirement 2 splits:

  ```text
  Exact stamp-copy contract:       PASSED
  Qdrant/Mongo integration path:   PENDING_DOCKER_VERIFICATION
  ```

  The `promote()` → Qdrant payload / Mongo mirror path was skipped outside
  the backend container; the copy contract is verified, the integration
  path is not yet.
- Missing-dependency suites are NOT_EXECUTED, neither product failures nor
  successful regression verification:

  ```text
  test_graph_promotion_jobs.py: NOT_EXECUTED_MISSING_TEST_DEPENDENCY    (pytest-asyncio)
  test_extraction_jobs.py:      NOT_EXECUTED_MISSING_RUNTIME_DEPENDENCY (motor)
  ```

Frozen stamp semantics (the ONLY interpretations later slices may use;
classifier `stamp_status` in `models/release_stamp.py`):

```text
release_stamp key absent            → legacy or uninstrumented artifact
release_stamp all-null              → instrumented before an active release registry
partially populated stamp           → some identities known; no missing value inferred
fully populated stamp               → recorded artifact identity, still non-authoritative
```

Three query concepts stay separate (never merged, never overwriting):

```text
QueryIR.release_pins              → desired or policy release context
QueryIR.release_stamp             → identity recorded when the query artifact was created
diagnostics.release_stamps_read   → identities actually observed on retrieved evidence
```

Entry conditions for the graph-promotion gate slice (run BEFORE enabling
even a default-off enforcement flag):

1. Persist a stamped extraction row.
2. Promote it through the real Mongo and Qdrant writers.
3. Read both resulting records.
4. Assert byte-equivalent release-stamp content.
5. Confirm an unstamped historical row still omits the key.
6. Run extraction-job and graph-promotion-job suites with their required
   dependencies.

**VERIFIED 2026-08-03** (Docker stack up, `polymath_v33-backend` rebuilt
with Step 3 code; script `tmp/step3_docker_gate_verification.py`):

```text
Gates 1-5: RESULT: ALL_GATES_PASSED
  - real _persist_extraction_rows; seam stamp = honest all-null pre_registry
  - real promote_doc receipt: {status: ok, promoted: 2}
  - Qdrant payload and Mongo chunks readback byte-equivalent
    (canonical-JSON comparison against the injected registry stamp)
  - legacy unstamped row omits release_stamp on BOTH stores
Gate 6: test_promote + test_extraction_jobs + test_graph_promotion_jobs
        + contract suites in-container: 116 passed
Cleanup verified: 0 residual rows, test collection deleted.
```

Next slice boundary (deny-by-default graph-promotion check):

- Uses the categorical `ReleasePin` + active release registry — NOT the
  non-authoritative artifact `ReleaseStamp`.
- Candidate classification and durable queue creation → remain allowed.
- Canonical Neo4j write execution → deny by default unless the active
  categorical release passes.
- Read paths and shadow inspection → unchanged.
- Null, partial, conflicting or missing artifact stamps → never grant
  write authority.
- Blocked target state (owner-frozen, `blocked_no_release_state`):

  ```json
  {
    "state": "blocked_no_release",
    "retryable": true,
    "write_attempted": false,
    "missing_conditions": [],
    "release_registry_entry": null
  }
  ```

  A blocked job remains durable and eligible for deterministic
  reconsideration after release promotion, without creating repeated
  failed write attempts.

---

## Stage 6 — Minimal integration design (real names and paths)

```
┌────────────────────────────── retained ──────────────────────────────┐
 routers/chat.py                    POST /api/chat entrypoint (unchanged)
 services/chat_orchestrator.py      process_chat_request (extended below)
 services/retriever/query_plan.py   QueryPlanV2 authoritative planner
 services/retriever/vocabulary.py   CorpusVocabularyResolver (internals frozen)
 services/retriever/__init__.py     RetrieverOrchestrator.retrieve_planned
 services/retriever/{tier0_router, four_lane_router,
   summary_tree_navigator, intent_policy, temporal, cross_domain,
   evidence_plan, evidence_allocation, planned_fusion, reservation_policy,
   hydrate, assembly}.py            all unchanged
 services/reranker.py               cross-encoder sole scoring authority
 polymath_mcp/*                     MCP sidecar + tools (facaded below)
 services/control_plane/*           ingestion Control Plane V2 (pattern source)
 models/contracts.py                B0 storage contracts (unchanged)
└──────────────────────────────────────────────────────────────────────┘

┌────────────────────────────── extended ──────────────────────────────┐
 services/retriever/query_plan.py   + IR fields: temporal compilation ref,
                                      ontology refs, obligation list,
                                      release_pins (additive, defaulted)
 services/retriever/temporal.py     + TemporalCompilation typed output w/
                                      explicit clock interpretation
 services/retriever/vocabulary.py   + return wrapped VocabularyResolutionBundle
 services/retriever/cross_domain.py + per-corpus budget accounting diagnostic
 services/chat_orchestrator.py      + plan-time outcome atoms, verification
                                      hook after SSE completion, release pins
                                      in trace events
 models/schemas.py                  + RetrievalResult.diagnostics keys
                                      (additive only)
 polymath_mcp/server.py             + tool-surface manifest registration
└──────────────────────────────────────────────────────────────────────┘

┌────────────────────────────── adapters added ────────────────────────┐
 models/query_ir.py                 QueryIR pydantic contract (extends
                                      QueryPlanV2 fields) + validation gate
 services/retriever/evidence_item.py EvidenceItem typed view over SourceChunk
                                      (+ RetrievalPayload refs)
└──────────────────────────────────────────────────────────────────────┘

┌────────────────────── new components genuinely required ─────────────┐
 services/retriever/ontology_resolution.py   query-time canonical mapping
                                              (reads frozen ontology.yaml +
                                              registries; consumes bundle)
 services/retriever/evidence_verification.py contradiction detection +
                                              post-synthesis claim audit
                                              (read-only, no re-retrieval)
 services/ontology_control/{ledger,proposals,
   activation}.py                             ontology proposal lifecycle
                                              (pattern copy of control_plane)
 routers/ontology_control.py                  proposal/approval endpoints
 registries/release_pins.v1.json              active release registry
└──────────────────────────────────────────────────────────────────────┘

┌────────────────────────────── deprecated ────────────────────────────┐
 (none — nothing is retired in slice 1-3; curation.py stays shadow-gated,
  ranking_policy stays default until heldout eval says otherwise)
└──────────────────────────────────────────────────────────────────────┘

Database projections affected:
  Mongo: + ontology_proposals, ontology_stage_attempts (new collections),
         + release_promotions (new collection). No change to existing
         collections. Qdrant/Neo4j: NONE.
Configuration affected:
  + registries/release_pins.v1.json (new file)
  + env flags: QUERY_IR_GATE_ENABLED (default off → warn-only → enforce),
    EVIDENCE_VERIFICATION_ENABLED (default off),
    ONTOLOGY_CONTROL_PLANE_ENABLED (default off)
Migrations required: NONE for slices 1-3 (additive). Slice 6 needs index
  creation on the three new Mongo collections (idempotent, startup-safe).

Invariants enforced by design:
  no unvalidated IR reaches a retriever ............ S1 gate
  raw user query not re-interpreted per retriever .. QueryPlanV2 single source
  no LLM-generated Cypher .......................... parameterized reads only (regression test)
  vocabulary expansion ≠ factual evidence .......... ontology refs advisory-only
  one corpus ≠ whole cross-domain budget ........... S5 accounting
  no temporal query w/o clock interpretation ........ S4 typed field
  no factual claim w/o exact evidence .............. S7 audit
  no hidden contradiction .......................... S7 surfaced in trace
  no graph write bypasses release policy ........... graph writes stay DISABLED
  no auto-activated ontology proposal .............. S9 approval state machine
  no frozen extraction change ...................... change-control rule
```

---

## Stage 7 — Implementation sequence (dependency order, testable slices)

### Slice 1 — Typed contract and release-metadata scaffolding, ZERO runtime behavior change
- **Purpose:** shared typed contracts + EvidenceItem view + release-pin stamps.
  This is scaffolding ONLY: the active release registry, promotion history,
  and graph-write enforcement belong to the later control-plane slice.
- **Existing files modified:** `services/retriever/__init__.py` (additive
  `release_pins` sub-dict in diagnostics — values already exist as version
  strings).
- **New files:** `models/query_ir.py`, `models/release_state.py`,
  `models/vocabulary_resolution.py`, `models/evidence_item.py` (all
  dependency-light; contracts live in `models/` beside B0).
- **Symbols changed:** new `QueryIR`, `ReleasePin`, `graph_write_allowed`,
  `ReadinessDecision`, `VocabularyResolutionBundle`, `EvidenceItem` classes.
  `CorpusVocabularyResolver.resolve` is NOT modified in Slice 1 — the bundle
  wraps via `from_resolution()` at call sites in a later step.
- **Schema migration:** none.
- **Backward compatibility:** pure additions; no call-site behavior changes.
- **Tests added:** `tests/test_query_ir_contract.py` (round-trip, validation
  failures), `tests/test_evidence_item.py`, extension of
  `tests/test_vocabulary_resolver.py` (wrapped output equality vs current dict).
- **Verification:** `pytest tests/test_query_ir_contract.py tests/test_evidence_item.py tests/test_vocabulary_resolver.py tests/test_vocabulary_cache.py` exit 0.
- **Expected observable output:** `resolution["release_pins"]` present in
  retriever diagnostics; all existing tests unchanged green.
- **Rollback:** revert commit; wrapper is isolated behind one return statement.

### Slice 2 — Query IR validation gate (warn-only → enforce)
- **Purpose:** invariant "no unvalidated Query IR reaches a retriever".
- **Existing files modified:** `services/retriever/query_plan.py` (emit IR
  fields), `services/retriever/__init__.py` (validate at `retrieve_planned`
  top, warn-only under `QUERY_IR_GATE_ENABLED=warn`),
  `services/chat_orchestrator.py` (build IR via `build_query_plan_v2`).
- **New files:** none.
- **Symbols changed:** `build_query_plan_v2` gains defaulted IR fields.
- **Schema migration:** none.
- **Backward compatibility:** `retrieve` (legacy shim at line ~4526) wraps into
  IR internally; MCP direct-retriever callers unaffected.
- **Tests added:** gate unit tests; regression that MCP `polymath_search`
  path still validates; determinism test (same query ⇒ byte-identical IR hash).
- **Verification:** full query-side suite green in Docker backend env.
- **Expected observable output:** `diagnostics["query_ir_gate"] = {status}`;
  after soak period flip to `enforce`.
- **Rollback:** env flag off; gate returns to pass-through.

### Slice 3 — Temporal compilation + outcome atoms into IR
- **Purpose:** explicit clock interpretation; plan-time outcome analysis.
- **Existing files modified:** `services/retriever/temporal.py`
  (`TemporalCompilation` output), `services/chat_orchestrator.py`
  (plan-time atoms reuse `_build_retrieval_answerability_gate` helpers).
- **New files:** none.
- **Tests added:** temporal clock-interpretation cases (now-relative,
  document-relative, absolute, none); atom determinism test.
- **Verification:** `pytest tests/test_temporal_query_routing.py tests/test_temporal_canonical_window.py` + new cases.
- **Expected observable output:** `diagnostics["temporal_compilation"]` always
  present, `interpretation` never absent.
- **Rollback:** fields defaulted; consumers tolerate absence.

### Slice 4 — Ontology query resolution adapter
- **Purpose:** canonical entity/predicate/class/role refs from vocabulary
  matches; vocabulary vs ontology distinction enforced by data flow.
- **Existing files modified:** `services/retriever/__init__.py` (insert call
  after vocabulary resolution, before `grounded_vocabulary_lanes`).
- **New files:** `services/retriever/ontology_resolution.py` (read-only over
  `config/ontology.yaml`, `registries/domain_registry.v1.json`,
  `registries/predicate_normalization.v1.json`).
- **Constraint:** advisory refs only; cannot alter scores, lanes' required
  flags, or reservations.
- **Tests added:** resolution over fixture bundle; proof that refs never
  enter `RetrievalPayload`; frozen-file hash unchanged assertion.
- **Verification:** new suite + existing vocabulary/routing suites green.
- **Expected observable output:** `diagnostics["ontology_resolution"]` with
  matched canonical refs and applicability tiers.
- **Rollback:** env flag `ONTOLOGY_QUERY_RESOLUTION_ENABLED` default off.

### Slice 5 — Evidence verification (contradiction + claim audit)
- **Purpose:** surface known contradictions pre-synthesis; audit streamed
  claims vs exact evidence spans post-synthesis.
- **Existing files modified:** `services/chat_orchestrator.py` (two hooks).
- **New files:** `services/retriever/evidence_verification.py`.
- **Constraint:** read-only over the SAME EvidenceItems the answer used;
  never re-retrieves, never mutates scores.
- **Tests added:** synthetic contradiction pair detection; claim-span audit
  fixtures; determinism.
- **Verification:** new suite + chat trace contract tests green.
- **Expected observable output:** `diagnostics["evidence_verification"]`
  (contradictions surfaced in trace, claim audit summary in turn record).
- **Rollback:** `EVIDENCE_VERIFICATION_ENABLED` off.

### Slice 6 — Ontology control plane + release pins registry
- **Purpose:** proposal → approval → pinned activation lifecycle.
- **Existing files modified:** `backend/main.py` (index creation on startup),
  `services/retriever/ontology_resolution.py` (reads approved overlay).
- **New files:** `services/ontology_control/{__init__,ledger,proposals,activation}.py`,
  `routers/ontology_control.py`, `registries/release_pins.v1.json`.
- **Schema migration:** create 3 Mongo collections + indexes (idempotent).
- **Backward compatibility:** with zero approved proposals, behavior =
  frozen `config/ontology.yaml` exactly (hash assertion in tests).
- **Tests added:** proposal lifecycle state machine; approval required for
  activation; rollback by de-pinning; frozen snapshot untouched.
- **Verification:** new suite green; `sha256 config/ontology.yaml` equals
  freeze-manifest hash `f484cb6a…`.
- **Expected observable output:** proposal endpoints under `/api/ontology/*`;
  activation recorded in `release_promotions`.
- **Rollback:** de-pin release entry; resolver falls back to snapshot.

### Slice 7 — Cross-domain budget accounting + MCP stable facade
- **Purpose:** hard per-corpus budget accounting; versioned MCP tool surface.
- **Existing files modified:** `services/retriever/cross_domain.py` /
  `planned_fusion.py` (accounting diagnostic), `polymath_mcp/server.py`
  (manifest registration), `polymath_mcp/tools.py` (per-tool version
  metadata).
- **Tests added:** budget accounting fixtures; facade manifest contract test
  (extends `tests/test_polymath_mcp_query_tools.py`).
- **Verification:** existing cross-domain + MCP suites green + new tests.
- **Expected observable output:** `diagnostics["corpus_evidence_budget"]`;
  `polymath_mcp_status` includes tool-surface manifest.
- **Rollback:** diagnostics are additive; manifest flag off.

Each slice is independently testable and ships behind an env flag with the
default OFF for anything behavior-affecting. Slice 1 is safe to start during
the closed-world annotation phase because it changes zero runtime behavior.

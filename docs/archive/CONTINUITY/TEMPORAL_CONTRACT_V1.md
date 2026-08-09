# TEMPORAL CONTRACT V1 — Normative Design Specification

Recorded 2026-08-03 by owner sequencing decision. This document is the
normative contract for the temporal slices T1–T5. Until a slice's runtime
enforcement is explicitly enabled, everything here is **descriptive only**:
the categorical release state remains the sole normative authority.

```yaml
status: proposed_baseline
authority: design_contract
runtime_enforcement: disabled

existing_authoritative_fields:
  - document_date
  - source_published_at
  - date_confidence
  - bibliographic_provenance
  - created_at
  - updated_at

compatibility_rules:
  document_date_preserved: true
  file_time_never_publication_time: true
  missing_dates_remain_null: true
  no_llm_date_inference: true
  no_network_date_resolution: true

new_contracts:
  temporal_envelope: pending_T1
  source_adapter_emission: pending_T2
  claim_valid_time: pending_T3
  chunk_inheritance: pending_T4
  query_clock_compilation: pending_T5
  recency_routing: pending_T5
```

## Envelope clocks

The envelope carries source-specific detail; Query IR selects the relevant
semantic clock family. Nine clocks, in canonical order:

```text
source_published_at
source_recorded_at
original_work_published_at
edition_published_at
electronic_edition_published_at
source_updated_at
transcript_generated_at
source_acquired_at
system_recorded_at
```

## Query-facing clock families

```text
valid time        → when a claim was true or the event occurred
source time       → when the source reported/recorded it
transaction time  → when Polymath recorded it
```

The larger envelope provides source-specific detail; Query IR selects the
relevant semantic clock. These three families must remain distinct in
Query IR (T5).

## Core semantics

- **Intervals are half-open.** `2024` → `[2024-01-01, 2025-01-01)`,
  granularity `year`. Month/day/quarter likewise.
- **Date-only values never become invented UTC timestamps.** Keep
  `{date, granularity, timezone_status}`; do not mint `00:00:00Z`.
- **Unknown stays null** with a reason code; never guessed.
- **Conflicting candidates remain present.** Selection is a deterministic
  projection (fixed precedence), not the destruction of competing metadata.
- **Relative expressions without a deterministic anchor** resolve to
  `unresolved_anchor` — preferable to a deterministic-but-false date.
- **Document time is not claim time.** A 2026 transcript saying
  "Microsoft acquired GitHub in 2018" carries `source_recorded_at = 2026`
  and claim `valid_time = 2018`; both remain on the claim record.
- **Deterministic guarantee:** same immutable source bytes + adapter
  version + precedence policy + temporal parser version + locale/timezone
  policy + extraction release + ontology/acceptance policy ⇒ same
  normalized dates, candidates, selected clock, claim temporal records,
  and artifact hashes.

## Per-source-type precedence policies

Versioned policy per source type (transcript, meeting transcript, EPUB,
research paper, news/article, Markdown notes, PDF, JSON/API record), each
ending in a versioned `Unknown` rung. Materialized in
`backend/registries/temporal_policy_registry.v1.json`
(executor-proposed, owner-ratifiable, changes require new version).
Filesystem modification time is only ever a low-authority fallback and
never silently becomes the publication date.

## Slice map

### T1 — Typed temporal contracts (models only, zero behavior change)

```text
backend/models/temporal_contract.py
backend/registries/temporal_policy_registry.v1.json
tests/test_temporal_contract.py
tests/test_temporal_policy_registry.py
```

No changes to: `bibliographic.py` behavior, format adapters,
`ChunkExtraction` output, Qdrant/Mongo payloads, retriever ordering,
`temporal.py` routing, Neo4j writes.

T1 invariants (tests fail unless all hold):

```text
A year normalizes to [year-01-01, next-year-01-01).
A month normalizes to [month-start, next-month-start).
A day normalizes to [day-start, next-day-start).
Unknown does not become an artificial interval.
Date-only values do not become invented UTC timestamps.
Conflicting candidates remain present.
Selection does not delete losing candidates.
Extra metadata cannot silently change the selected clock.
Canonical JSON is stable.
Existing bibliographic fields remain representable.
No runtime module imports the new contract yet.
```

### T2 — Adapter emission

`bibliographic.py` and source adapters populate `TemporalEnvelope`; legacy
`document_date` preserved as a compatibility projection. Runs behind
`TEMPORAL_ENVELOPE_ENABLED=off|shadow|enforce`, initially `shadow`.

### T3 — Post-extraction claim-time enrichment (corrected scope)

Do NOT implement claim `valid_time` by modifying the frozen extraction
decision logic. Introduce a post-extraction seam:

```text
frozen ChunkExtraction → temporal_claim_enricher → EvidenceItem temporal projection
```

Location: `backend/services/temporal/claim_temporal_enrichment.py`.
Consumes existing chunk time expressions, the document temporal envelope,
claim evidence offsets, sentence boundaries, and deterministic anchor
rules; emits `valid_time`, `temporal_resolution_status`,
`temporal_anchor_id`, `temporal_rule_version`. Entity detection, relation
proposals, predicate decisions, acceptance lanes, and extraction
thresholds are untouched. Any later change inside
`backend/services/extraction/` still follows the frozen change-control
procedure (documented failure → positive fixture → adversarial negatives →
ablation → adjudication → benchmark impact).

### T4 — Storage and hydration

Mongo: complete envelope + provenance. Qdrant: selected filterable date
fields only. Hydration: document temporal context + claim temporal
projection.

### T5 — Query IR clocks and recency

Absolute temporal constraints + selected query clock + `query_as_of` +
recency directive → qualified temporal retrieval. The Query IR extension
attaches to `QueryPlanV2`; it does NOT create another planner. Recency
(latest/newest/most recent) requires: clock selection, semantic candidate
retrieval, exclusion of invalid/future-dated records, newest-first
ordering of qualified candidates, and verification that the answer used
the newest qualifying evidence.

## Gate between T1 and runtime work

```yaml
temporal_spec_recorded: passed          # 2026-08-03, this document
temporal_contract_models: passed        # models/temporal_contract.py, 42 in-container
temporal_contract_roundtrip: passed     # canonical JSON byte-stable (suite above)
temporal_policy_registry: passed        # frozen sha256 pin + structural validator
runtime_behavior_change: none
storage_change: none
extraction_change: none
query_change: none
import_isolation: passed                # AST scan: only models/ + tests/ reference T1
adjacent_regression: passed             # release_stamp_step3 + registry_loader + contracts, 55 passed
```

## Mandated sequence

```text
NOW
├── Record TEMPORAL_CONTRACT_V1.md          ← this document
└── Implement T1 contracts and tests

NEXT BEHAVIOR CHANGE
├── Docker stamp verification               ← ALREADY PASSED 2026-08-03
│                                             (QUERY_MINIMAL_IMPLEMENTATION_PLAN.md:
│                                             docker_promotion_integration: passed)
└── Add deny-by-default canonical graph-promotion enforcement

GRAPH-PROMOTION RELEASE GATE — IMPLEMENTED 2026-08-03:
```yaml
slice: deny_by_default_graph_promotion_enforcement
boundary: services/ingestion/graph_promotion_jobs.run_graph_promotion_jobs
flag: GRAPH_PROMOTION_RELEASE_GATE=off|shadow|enforce   # default off
authority: models/release_state.ReleasePin exclusively
blocked_shape: blocked_no_release_state() (owner-frozen)
regression: 114 passed in-container (gate suite 17 new + extraction/promotion/release suites)
unchanged_by_design:
  - candidate classification (classify_graph_promotion_candidate)
  - durable job creation (plan_graph_promotion_jobs)
  - shadow records / graph inspection
enabled_in_production: not yet (flag remains off until shadow verification)
```

GLOBAL GRAPH-WRITE ENFORCEMENT — CLOSED 2026-08-03 (manual repair gated):
```yaml
slice: shared_authorization_seam_manual_repair_closure
shared_seam: services/ingestion/graph_promotion_jobs.authorize_canonical_graph_write
  # returns GraphWriteAuthorization(allowed, enforcement_applied, decision);
  # consumed by BOTH write surfaces — neither interprets ReleasePin directly
write_surfaces_gated:
  - run_graph_promotion_jobs           (durable-job boundary)
  - ingestion_service.backfill_graph_failures  (operator manual repair)
manual_repair_blocked_behavior:
  contract: blocked_no_release_state() verbatim + gate_mode/release_gate/operator metadata
  neo4j_calls: 0
  write_attempt_increments: 0
  repair_candidate_state: preserved (no readiness side effect, retry eligibility intact)
  classification: a release block is NOT a graph failure
regression: 109 passed in-container (manual-repair suite 10 new incl. frozen
  AST no-bypass scan + gate/promotion/release/readiness suites)
canonical_neo4j_caller_enumeration:
  gated: [graph_promotion_jobs.py, ingestion_service.py]
  definitions: [graph_backfill.py, promote.py]
  frozen_script_exceptions_pending_gate_decision:
    - scripts/polymath_failed_chunk_backfill.py
    - scripts/promote_backfilled_relations.py
    - scripts/polymath_graph_replay_backlog.py
  routers: [routers/ingestion.py (calls gated service methods only)]
global_no_bypass_invariant: enforced by frozen AST allowlist in
  tests/test_manual_repair_release_gate.py — any NEW production caller of
  backfill_failed_graph_chunks / promote_claims_to_graph fails the suite
rollout_order:
  - shadow on live traffic, inspect would-block/would-allow records
  - active release-registry loader (active_release_pin() currently None by design)
  - enforce only after shadow evidence clean AND a qualifying active ReleasePin exists
enabled_in_production: not yet (flag remains off)
```

LEGACY CLI WRITE SURFACES — CLOSED 2026-08-03 (scripts gated, exceptions removed):
```yaml
slice: legacy_cli_script_gating
scripts_gated:
  - scripts/polymath_failed_chunk_backfill.py
  - scripts/promote_backfilled_relations.py
  - scripts/polymath_graph_replay_backlog.py
routing: CLI -> evaluate_cli_graph_write_gate (shared seam, once per run)
  -> blocked: frozen blocked_no_release payload + exit code 77
     (RELEASE_POLICY_BLOCK_EXIT_CODE) BEFORE any client connection, state
     mutation, counter update, or Neo4j call; operator + script identity recorded
  -> allowed: per-doc writes through gated service method
     ingestion_service.backfill_graph_failures (allow_extraction passthrough
     added for promote_backfilled_relations; never re-runs Ghost B)
off: existing execution preserved exactly
shadow: would_block/would_allow recorded in summary/run records, execution continues
enforce: blocks before anything; no --unsafe-bypass/--force/env override exists
regression: 137 passed in-container (27 new subprocess-level CLI scenarios
  against the live stack incl. unreachable-Mongo proof of pre-connection
  blocking + strict AST suite + full release-control suites)
global_no_bypass_invariant: STRICT — AST exception list deleted; no production
  module or script imports the canonical writers except the authorized seams
  {graph_promotion_jobs.py, ingestion_service.py}; definitions and test fakes
  excluded; remaining text matches are prose (routers/ingestion.py docstring,
  promote_relations_targeted.py comment)
bug_class_fixed: models/release_state.missing_release_conditions /
  graph_write_allowed now deny any non-ReleasePin value (malformed registry
  entries can never crash the gate into a permissive path)
status:
  service_and_api_write_surfaces: closed
  legacy_cli_write_surfaces: closed
  global_no_bypass_invariant: passed
next: Shadow A (active_release_pin() = None) -> registry loader -> Shadow B
  -> bounded canary enforce -> general enforce -> T2
enabled_in_production: not yet (flag remains off)
```

## SHADOW A — MISSING-RELEASE DENY PATH — PASSED (2026-08-03)

```yaml
slice: Shadow A — gate=shadow, active_release_pin() -> None
purpose: prove the missing-release deny decision and observability path ONLY;
  the future allowed-release path is NOT proven here
runner_hardening: release state now resolved ONCE at run start
  (run-level authorization hoisted out of the per-job loop); failure path
  also records release_gate_shadow — no execution without an authorization
  record; run-level shadow record carries gate_mode / resolved_once /
  registry_entry=null / caller / operator
identity_records: all five surfaces now persist caller + operator next to
  their shadow decision (runner run record, service result, CLI summaries +
  run records + promote reports)
probe: backend/scripts/shadow_a_graph_release_gate_probe.py — runs every
  canonical surface once per mode (off, shadow) against a disposable test
  corpus (11111111-2222-4333-8444-555555555555), strips additive/volatile
  diagnostics, compares candidates/executions/results/counters/Neo4j
  mutations; fixtures cleaned after capture
report: data_eval/graph_promotion_release_gate_shadow_a.json
  (graph_release_gate_shadow_a.v1) — status: passed
acceptance (all met):
  gate_mode: shadow
  active_release_entries: 0
  observed_gate_decisions: would_block_only
  canonical_write_invocations_observed: 5/5
  canonical_write_invocations_without_decision: 0
  canonical_writer_callers_outside_gate: 0
  execution_blocked_by_shadow: 0
  shadow_induced_state_changes: 0
  shadow_induced_counter_changes: 0
  shadow_induced_graph_failures: 0
  off_vs_shadow_candidate_drift: 0
  off_vs_shadow_execution_drift: 0
  off_vs_shadow_result_drift: 0
  missing_release_decisions_complete: true
  operator_identity_present: true
  caller_identity_present: true
  run_release_resolved_once: true
  authorizations: 5 = executions_or_policy_decisions: 5
regression: 92 passed (release-gate + manual-repair + legacy-CLI +
  release-state-contract + promotion-jobs suites) in-container
failure_conditions: none triggered (no exit 77 in shadow; no mixed
  decisions; single per-run resolution)
record:
  shadow_a_missing_release_path: passed
  shadow_a_execution_drift: 0
  shadow_a_unobserved_write_surfaces: 0
next: fail-closed active release-registry loader (8 fail-closed conditions,
  trace identity JSON, resolved once per run) -> Shadow B -> bounded canary
  enforce -> general enforce -> T2
enabled_in_production: not yet (flag remains off; enforce would correctly
  block every canonical graph write while the pin is still None)
```

## RELEASE REGISTRY LOADER + SHADOW B — PASSED (2026-08-03)

```yaml
slice_1A: fail-closed active release-registry loader
registry: backend/registries/release_pins.v1.json — schema_version
  release_pins.v1, entries: [] (zero active entries is the honest
  pre-qualification state; deny-by-default until a ReleasePin is issued)
loader: backend/services/control_plane/release_registry.py
  load_release_registry() resolves exactly one valid active entry or fails
  closed with the exact reason: registry_missing, registry_malformed,
  unsupported_version, zero_active_entries, multiple_active_entries,
  malformed_active_entry, categorical_state_missing,
  categorical_state_failed, entry_hash_missing, entry_hash_mismatch.
  ReleasePin extra="forbid" — ReleaseStamp / TemporalEnvelope payloads are
  rejected, never inferred. entry_hash = sha256 over canonical entry JSON
  minus entry_hash; registry_hash = sha256 of raw bytes. Output carries
  registry_version / registry_hash / entry_id / entry_hash / release_pin.
run_handle: begin_registry_run() pins the resolution + registry hash at
  run start; verify_unchanged() fails closed on mid-run mutation — the
  runner stops remaining jobs as blocked_registry_mutated BEFORE lease,
  counters, or Neo4j; decisions never refresh mid-run so they cannot mix
wiring: active_release_pin() is now registry-backed for every surface;
  runner resolves via begin_registry_run when no explicit release is
  passed; config RELEASE_REGISTRY_PATH overrides the default path
trace: run-level shadow/enforce decision relays release_registry_entry +
  full registry_identity and registry_resolution reason (a fail-closed
  registry is distinguished from an evaluated failing pin)
tests: tests/test_release_registry_loader.py — 24 tests: full fail-closed
  matrix, hash canonicalization, mutation/deletion mid-run, enforce
  allow + enforce blocked (queued, zero write attempts), resolve-once
  counting, mutation stops remaining jobs
combined_regression: 116 passed (gate + manual-repair + legacy-CLI +
  release-state-contract + promotion-jobs + registry-loader suites)
shadow_b_probe: backend/scripts/shadow_b_graph_release_gate_probe.py —
  four phases against the live stack: baseline_off, shadow_allow (valid
  active pin), shadow_block (failing pin), shadow_mixed (multi-job run);
  registry identity relayed via RELEASE_REGISTRY_PATH for CLI surfaces
report: data_eval/graph_promotion_release_gate_shadow_b.json
  (graph_release_gate_shadow_b.v1) — status: passed
acceptance (all met):
  valid_release_would_allow: passed
  invalid_release_would_block: passed
  registry_resolved_once_per_run: passed
  mixed_release_decisions_in_one_run: 0
  shadow_execution_drift: 0
  unobserved_write_surfaces: 0
next: bounded canary enforce (disposable corpus: invalid release -> no
  lease/write; valid release -> one idempotent execution; blocked jobs
  stay queued/retryable; duplicate execution creates no duplicate graph
  state) -> general enforce -> closed-world qualification -> T2
enabled_in_production: not yet (GRAPH_PROMOTION_RELEASE_GATE remains off;
  the shipped registry holds zero active entries)
```

## 1C CANARY ENFORCEMENT — PASSED (2026-08-03)

```yaml
probe: backend/scripts/canary_graph_release_gate_probe.py — four phases
  under GRAPH_PROMOTION_RELEASE_GATE=enforce on disposable corpus
  33333333-4444-4555-8666-777777777777; isolated fixture registries via
  RELEASE_REGISTRY_PATH — the shipped zero-active-entry registry was never
  modified
audit_helper: graph_promotion_jobs.instrument_canonical_writer_calls —
  writer tripwires now owned by the authorized execution module; probes
  consume the helper instead of importing writers (AST no-bypass
  invariant stays strict, zero new exceptions)
phase_A_invalid_release: zero-active registry -> all five surfaces block:
  4/4 durable jobs blocked_no_release BEFORE lease (queued, attempts 0,
  retryable), manual repair frozen blocked payload, 3/3 CLI exit 77 with
  frozen payloads, ZERO writer executions (tripwire), Neo4j corpus counts
  unchanged
phase_B_valid_release: fully-passing fixture pin -> run-level would_allow
  + resolved_once, 4/4 jobs leased (attempts +1) and completed noop,
  manual repair executed, 3/3 CLI --apply exit 0; one decision across the
  whole run (mixed_registry_decisions_per_run: 0)
phase_C_duplicate_replay: same durable job id requeued + executed again,
  runner re-run planned 0, manual repair + CLI apply replayed -> Neo4j
  counts identical before/mid/after, final state unchanged
phase_D_promotion_reconsideration: job d2 blocked under the invalid
  registry (queued + last_release_gate blocked_no_release), then the SAME
  job id reconsidered once after the override flipped to the passing
  fixture -> allowed -> noop; replacement_jobs_created: 0
report: data_eval/graph_promotion_release_gate_canary.json
  (graph_release_gate_canary.v1) — status: passed
closeout (all met):
  graph_gate_canary_invalid_release: passed
  graph_gate_canary_valid_release: passed
  blocked_job_retryability: passed
  release_promotion_reconsideration: passed
  enforced_allow_idempotency: passed
  neo4j_writes_while_blocked: 0
  mixed_registry_decisions_per_run: 0
  unobserved_write_surfaces: 0
regression: 116 passed (all six release-control suites) in-container
next: general enforce decision (the shipped registry holds zero active
  entries, so enforcement blocks every canonical graph write — matching
  the authoritative state that graph writes stay disabled until
  qualification) -> closed-world qualification -> real ReleasePin -> T2
enabled_in_production: not yet (GRAPH_PROMOTION_RELEASE_GATE remains off)
```

THEN
├── T2 adapter emission (shadow first)
├── T3 post-extraction claim temporal enrichment
├── T4 storage/hydration
└── T5 Query IR clocks, confidence policy and recency ranking
```

Temporal stamps and envelopes remain descriptive until their corresponding
runtime slices are explicitly enabled. This ordering preserves the
extraction freeze and does not displace the release-control dependency
chain.

---

## TWO-DOCUMENT SMOKE FIXTURE — RAN (2026-08-03)

Report: `data_eval/temporal_two_document_smoke_20260803.json`
Probe: `tmp/temporal_smoke_probe.py` + raw `tmp/temporal_smoke_retrieval.json`
Corpus: `0f04781e-0d78-4810-9301-7d8029cd598b` (Temporal Smoke Fixtures)

Benesh 1975 OCR article + TikTok Shop 2026 transcript ingested separately
through the production durable-batch upload path with a 362 s knowledge-time
gap; both reached the full ladder (queryable + graph_extracted +
graph_promoted, extraction 65/65 and 91/91 under legacy_local sidecar).

Verified working now:
- frontmatter date capture: tiktok `document_date=2026-07-13`, method
  `frontmatter_date`, precision day; `source_kind=youtube_video`.
- deterministic routing: 6/7 owner "now" queries returned the correct
  document at rank 1 with zero cross-document mixing; all evidence carried
  doc_id + chunk_id.
- transaction-time testability: distinct knowledge clocks 18:12:27Z vs
  18:18:29Z.

Exposed gaps (all feed T2–T5 backlog, none blocked by release control):
1. Frontmatter stripped before chunking — captured document_date has no
   retrievable evidence; the "dated July 13, 2026" query misrouted to the
   other document. T4 must make document clocks queryable.
2. Benesh `document_date=null` (`no_date_source`) — bibliographic detector
   lacks the T&F/libgen citation-header pattern; 1975/2012/2015 survive
   only as chunk text with no role separation.
3. T1 fields exist but emit nulls: `temporal_class`, `time_expressions`,
   `page_start/page_end`, `quality_flags` — the 1955–1973 chronology, the
   relative expressions, and the reversed page-3 OCR noise are all
   unflagged. T2 adapter emission is the fix.
4. Environmental: global extraction engine `local` fail-closes with an
   empty provider-card pool (legacy_local used for this fixture); chat
   answer routing hit intermittent litellm 401s and over-cautious refusal
   wording on completed turns.

# Book Ingestion P4 — Route-Aware Readiness, Self-Healing, Certificates

Date: 2026-07-27 · Phase: P4 of the repair/production-qualification program
Fixture corpus: `f842e3b5-8de3-4eaf-9785-9bf20e3e13e4` ("Temporal Smoke Relex-Only")
Evidence: `data_eval/relex_extraction_lane.json`, `data_eval/route_readiness_certificate_f842e3b5.json`

## Objective

Close the readiness loop for the fixture corpus after the deterministic-summary
phases (P1–P3) and the Relex re-extraction lane, then make readiness
route-aware and enforcement-capable at every query entry point per spec §4.2–§4.5.

## What was done

### 1. Extraction convergence (Relex lane, no relabeling)

- Sole readiness blocker was `extraction_jobs_pending` (26 queued) from
  extraction-contract hash drift. Owner decision: re-extract through the Relex
  lane; never relabel historical rows.
- New script `backend/scripts/run_relex_extraction_lane.py`: engine guard
  (refuses non-`relex_local` corpora), sidecar preflight
  (`model_hash_verified: true`, model `knowledgator/gliner-relex-large-v1.0`,
  hash `7c5bd751…`, device mps), job cycles, final plan apply.
- Live result: 26/26 jobs succeeded in 2 cycles. 91/91 extraction rows are
  Relex-stamped (`provider: relex_local` + `local_extraction.*`); contract
  hashes: old `e66d8c7…` (65 rows, untouched history) + new `196b15ac…`
  (26 rows, current contract). Readiness advanced `extraction_pending` →
  `fully_enriched` (terminal).

### 2. §4.2 route-aware readiness wiring

New bridge module `backend/services/ingestion/route_readiness.py`:

- `corpus_artifact_census` translates the materialized readiness row into
  artifact facts (`child_vectors`, `parent_records`, `summary_records`,
  `evidence_obligations`, `entity_lexicon`, `extraction_artifact`,
  `graph_projection`) plus registry-derived pins (`release_pin_match`,
  `graph_write_promotion`, …). Never raises at entry points.
- Feeds `ReadinessDecision.decide()` (models/release_state.py), which
  previously had zero production callers.

Wired into all four named entries:

| Entry | File | Behavior |
|---|---|---|
| MCP search | `polymath_mcp/tools.py` | `readiness_decisions` relayed in search payload; retrieval proceeds |
| MCP graph | `polymath_mcp/tools.py` | `graph_read` gate; all-blocked → structured `graph_not_ready`; discover only on allowed corpora |
| Chat | `services/chat_orchestrator.py` | `curated_chat` trace event after query-plan trace |
| Direct retriever | `services/retriever/__init__.py` | `retrieve(..., readiness_route=...)` attaches decisions to diagnostics on hit + miss paths |

Live split on fixture corpus: vector_search / hybrid_search / curated_chat /
entity_lookup = **full + allowed**; graph_read = **partial** (missing
`release_pin_match`); graph_write = **blocked** (registry ships
`entries: []` → fail-closed `zero_active_entries`). Ordinary retrieval is
unaffected while graph writes stay blocked.

### 3. §4.3 self-healing reconciler

- `backend/services/ingestion/graph_promotion_jobs.py`:
  `reevaluate_stale_graph_promotion_jobs` runs on every applied graph plan.
  Blocked/queued jobs are reconsidered when their dependency appears; jobs
  whose gap resolved become `noop` with an exact reason
  (`graph_gap_resolved`, `neo4j_disabled`, `document_gone`); jobs still
  missing artifacts stay blocked with counts exposed. Release-gated jobs are
  never touched by the reconciler.
- Live proof: both stale fixture jobs (`queued` + `blocked_no_extractions`)
  → `noop`/`graph_gap_resolved`; readiness repair census now reports
  `{"noop": 2}`. Summary-job self-heal from P3 covers blocked summary states.
- Dead-letter posture: fixture dead-letter total is 0; per-lane queue
  telemetry is exposed through readiness `queue_telemetry`; explicit repair =
  plan/run lane commands (extraction lane script above; summary lane from P3).

### 4. §4.4 corpus certificate

`corpus_certificate.v1` materialized live and persisted to
`corpus_certificates` (upsert by corpus_id; `certificate_id` = sha256 of
canonical body). Contains: corpus generation, source census, Relex release
(model hash verified), ontology release `f484cb6a…`, acceptance policy
`ebe3a82d…`, summary algorithm release (`deterministic_summary.v1`,
provider_required: false), embedding release
(`qwen3-embedding-0.6b-v1`, dim 1024), Qdrant projection release, graph
projection release (gate `enforce`, `active_release: false`,
registry reason `zero_active_entries`), and all six route verdicts.

### 5. §4.5 release gate

`GRAPH_PROMOTION_RELEASE_GATE=enforce` remains the shipped posture. The
release registry (`registries/release_pins.v1.json`) carries no active
entries; graph routes fail closed by design until the owner issues a release
pin. No code path bypasses the gate.

## Verification

- New tests `backend/tests/test_route_readiness_certificate.py` — 7/7 pass
  (census translation, route split, never-raise payload, stale-job noop,
  blocked-job requeue on dependency, blocked-job stability, certificate
  completeness).
- `tests/test_polymath_mcp_query_tools.py` — 8/8 pass after adapting the
  multi-corpus forwarding test to the new gate (monkeypatched allowed
  decision) and adding `test_mcp_graph_discover_blocked_without_route_readiness`
  (fail-closed contract: `graph_not_ready`, discover never invoked,
  missing artifact named).
- Broad regression (`retriev|chat|summary|graph_promotion|release|readiness|route_readiness`):
  809 passed / 15 failed. All 15 triaged pre-existing and environment-bound:
  13× `ServerSelectionTimeoutError: mongodb:27017` (subprocess tests need the
  docker hostname), 1× `.env` overrides a shipped default
  (`CHAT_COST_TELEMETRY_ENABLED`), 1× test fake `find_one(query)` lacks the
  projection arg used identically at HEAD (`batches.py` worker loop). None
  touch P4 code paths.

## Verdict

| Verdict | Result |
|---|---|
| readiness_terminal (`fully_enriched`) | true |
| ordinary routes allowed | true |
| graph_read blocked by release | true |
| graph_write blocked by release | true |

## Handoff to P5

Relex-only runtime gate + corpus estate migration: verify exactly one
registered production engine, reject legacy engine values for new ingest,
idempotent estate migration, quarantine historical artifacts (never relabel).

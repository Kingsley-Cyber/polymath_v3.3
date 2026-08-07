# Graph Projection Control-Plane Closure — CLOSEOUT (2026-08-04/05)

**Slice:** `graph_projection_control_plane_and_orphan_reconciliation`  
**Corpus:** `q9_10_file_inspection` (`6a766597-29f3-4a3e-8918-5de10f0053b3`)  
**Deferred (unchanged):** ontology release control plane; schema→entity join; production migration  
**Prohibited honored:** blind full re-extraction

## What shipped

| Component | Path |
|---|---|
| Job ledger + deterministic IDs | `backend/services/graph/projection_jobs.py` |
| Authorize / apply / verify / certify runner | `backend/services/graph/projection_runner.py` |
| Worker ledger hook (post-write, non-fatal) | `backend/services/ingestion/worker.py` |
| Unit tests | `backend/tests/test_graph_projection_jobs.py` (11 passed) |
| Ops close script | `backend/scripts/graph_projection_control_plane_close.py` |

### Lifecycle

`PLANNED → INPUTS_VALIDATED → ONTOLOGY_RESOLVED → AUTHORIZED → APPLYING → APPLIED → VERIFIED → CERTIFIED`

Terminal non-success used on q9: `NOOP_NO_ELIGIBLE_ARTIFACTS`, `BLOCKED_INPUT_MISSING`.  
`capability_eligible` only on `CERTIFIED`. NOOP never advertises readiness.

### Deterministic identity

`graph_job_id = hash(corpus_id + corpus_generation + document_id + lane + input_artifact_hash + ontology_release + projection_release)`

## Orphan reconciliation (MEASURED)

Population was `queued`/`running` (not still labeled `stuck_orphan`) — 2583 jobs with **no** `ghost_b_extractions` row.

| Class | Count | Disposition |
|---|---:|---|
| `missing_extraction` | **2583** | `REVIEW_MISSING_EXTRACTION` (`hold_no_blind_reextract`, `bounded_reextract_limit=0`) |
| `recoverable_projection` | 0 | — |
| `superseded_generation` | 0 | — |
| `deleted_document` | 0 | — |
| `legacy_engine_or_contract` | 0 | (hex contract hashes are not legacy labels) |
| `corrupt_or_ambiguous` | 0 | — |

Promoted covered chunks left alone: 157 `promoted` + 3 `skipped`.

Workers were stopped during classification to prevent blind re-extract of the queue (500 had already entered `running` before hold).

## Projection jobs (MEASURED)

| Status | Count |
|---|---:|
| `CERTIFIED` | 20 |
| `NOOP_NO_ELIGIBLE_ARTIFACTS` | 18 (mostly `qualified_facts` Fact=0) |
| `BLOCKED_INPUT_MISSING` | 2 (docs with Mongo chunks but no Neo4j/staging — not advertised ready) |

## Acceptance gates (MEASURED)

```yaml
jobs:
  pending: 0
  leased: 0
  stuck_orphan: 0
  unclassified_orphans: 0
  terminal_or_explicitly_blocked: true
determinism:
  duplicate_job_ids: 0
  restart_replay_identical: true
  idempotent_projection: true
verification:
  assertion_support_orphans: 0
  every_assertion_has_supporting_chunk: true
  no_op_marks_ready: false
readiness:
  advertised_mode: graph_assertion
  structural_ready: true
  entity_ready: true
  assertion_ready: true
  qualified_fact_ready: false
neo4j_counts:
  document_nodes: 8
  chunk_nodes: 160
  entity_nodes: 2310
  mention_edges: 3155
  relates_to_edges: 1230
  assertion_nodes: 36
  fact_nodes: 0
```

Receipt: `data_eval/q9_final/graph_projection_control_plane_closeout.json`

## Next (owner order unchanged)

1. Schema → corpus entity join  
2. Ontology release control plane  
3. Bounded production Graph canary — only after both are deterministic  

**NOT authorized:** production migration; blind full re-extraction of the 2583.  
Bounded re-extract of `REVIEW_MISSING_EXTRACTION` requires an explicit owner budget (`bounded_reextract_limit > 0`).

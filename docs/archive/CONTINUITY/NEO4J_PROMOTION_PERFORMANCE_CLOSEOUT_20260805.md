# Neo4j Promotion Performance Closeout (2026-08-05)

**Phase 2 of the ingestion speed-first plan: remove the ~78s/document fixed Neo4j
promotion overhead without changing graph semantics.** All changes regression-green;
end-to-end delta is measured in Phase 6 (baked benchmark on the same five files).

## Measured overhead split (corpus 0189427c: 5 docs / 2707 chunks / 24588 entities)

| Contributor | Cost | Was paid | Fix |
|---|---|---|---|
| `inspect_graph_capabilities` (7 corpus-wide MATCH scans) | **5.711s** MEASURED | **per doc** | deferred to once-per-batch |
| `certify_corpus_capabilities` | corpus-wide write | per doc | deferred to once-per-batch |
| redundant second clear (`_clear_document_graph_payload` after worker `delete_document_graph`) | full prune + delete + **global orphan sweep** | per doc | `skip_preclear=True` elides it |
| global orphan sweep `_delete_orphan_entities` (`MATCH (e:Entity)`) | full-graph scan over 24588 entities / 54933 mentions | per delete (×2 with double clear) | scoped to doc's `affected_entity_ids` |
| per-mention `resolve_ontology_metadata` in `summarize_dominant_facets` | recompute per mention | per entity | `lru_cache` (pure fn, frozen ontology) |

The per-doc capability inspect+certify was the dominant repeated cost: **5.711s × N
docs of identical corpus-wide scanning**. On the 5-doc bench batch that is ~28.6s of
pure repeat, before the double-clear's global orphan sweeps.

## Changes (all preserve graph semantics)

1. **Deferred corpus certification** — `projection_runner.run_projection_jobs` gains a
   `certify` gate (default True for reconcilers). `project_document_via_control_plane`
   passes `certify=False` (per-doc hot path). New `certify_corpus_once` runs the corpus
   inspect+certify a single time; `batches.py` calls it at end-of-batch (terminal state),
   after all docs' graph writes, non-fatal on failure. The certificate still runs — once,
   not N× — and reflects the complete corpus.
2. **Dropped the redundant second clear** — `write_document_graph(..., skip_preclear=True)`
   from the worker (which already ran `delete_document_graph`). The second prune+delete+
   global orphan sweep is elided. Default remains False so every other caller is unchanged.
3. **Scoped orphan sweep** — `_delete_orphan_entities(session, entity_ids=...)` restricts
   the sweep to the doc's pre-delete entity ids when non-empty; global sweep retained for
   corpus deletes / repair / the empty-candidate fallback (preserves the delete contract
   the tests assert).
4. **Cached ontology resolution** — `resolve_ontology_metadata` now wraps an
   `lru_cache(maxsize=32768)` core returning immutable items; each call builds a fresh
   dict (no shared mutable state). Pure deterministic function, frozen ontology.
5. **Delta skip** — NOT added as a separate hash check: the worker already skips the
   entire neo4j phase when `write_state.neo4j_written` (worker.py:4970, 1505, 1520), and
   projection jobs already NOOP on unchanged `input_artifact_hash`. A third early-exit
   would be redundant.

## Instrumentation added

- `phase=neo4j_timing` (worker): `delete_document_graph_s`, `project_via_control_plane_s`.
- `phase=projection_timing` (runner): `execute_s`, `certify_s`, `certify_deferred`.

## Regression evidence (run against the NEW code in the backend container)

- neo4j suite: **43 passed** — test_graph_payload, test_neo4j_write_via_projection_cp,
  test_neo4j_writer_explains, test_neo4j_writer_facts, test_pt8_idempotency.
- broader battery: **17 passed** — test_neo4j_writer_deadlock_retry,
  test_graph_authority_separation, test_corpus_qualified_identity.
- wiring verified by container import smoke (certify defaults, skip_preclear, orphan
  entity_ids param, timing markers).

## Parity gates

graph_projection_parity / unsupported_new_relations: no semantic change to what is
written — UNWIND write batching untouched, certification retained (once/batch), orphan
cleanup retained (scoped). The final `graph_projection_parity == 1.0` and
`unsupported_new_relations == 0` assertion runs in Phase 6 against the baked images on
the same five files; this closeout records the structural change + unit parity.

**Quality laws held:** no threshold change, no skipped work reported complete
(certification moved, not dropped), no direct Neo4j writes outside the control plane,
no docker-cp final state (hot-patch was measurement-only; Phase 6 bakes).

# Book Pipeline Repair Baseline — 2026-08-03 (pre-repair freeze)

Frozen before any production behavior change in the deterministic-summary repair
program. Full machine-readable census: `data_eval/book_pipeline_repair_baseline.json`.

## Git

- SHA: `86149227db703c097aad586e4a18018185e9f742`
- branch: `extraction/deterministic-recall-ladder`
- dirty tree: 214 modified/untracked files (pre-existing working-tree state; none
  introduced by this program yet)

## Graphify baseline

- Snapshot: `graphify-out.pre-book-pipeline-repair/` (copy of `graphify-out/`,
  built from commit `86149227` on 2026-08-03)
- 16,449 nodes · 41,945 edges · 456 communities

## Store census (live, captured 2026-08-03)

### Mongo (`polymath` db)

| fact | value |
|---|---|
| documents | 659 (active 651) |
| chunks | 395,412 |
| ingestion_runs | reconciling 213 · query_ready 357 · excluded 4 |
| query_ready_certificates | 357 |
| corpus_readiness records | 24 |
| corpus engine values | relex_local 11 · runpod_flash 16 · cloud 1 |
| document engine stamps | relex_local 7 · runpod_local_extraction 613 · none 39 |
| extraction models | urchade/gliner_medium-v2.1: 128,933 · gliner-relex-large-v0.5: 256 · gliner-relex-large-v1.0: 194 · unstamped/blank: 233,754 |

Job queues: source_parse {succeeded 1539, skipped 11, superseded 3} ·
extraction {queued 1702, promoted 2196, skipped 35154, superseded 583} ·
summary {queued 13150, succeeded 22398, superseded 12392, running 1,
blocked_no_parent_summaries 69} · graph_promotion {done 289, noop 103,
blocked_no_extractions 9, queued 5, failed 1, superseded 2} ·
document_pipeline {succeeded 67, superseded 75, queued 15,
blocked_mongo_state 8, dead_letter 4, failed 2, skipped 3}.

### Qdrant

Collection list + point counts in baseline JSON (per-corpus `*_naive`,
`*_graph`, `*_hrag` topology — identical child points triplicated, see audit §6).

### Neo4j

Constraints + per-label counts in baseline JSON. Graph writes remain
release-gated (`GRAPH_PROMOTION_RELEASE_GATE=enforce`).

## Existing suite results (pre-repair)

- `pytest tests/ -k "summary_tree or deterministic_summary or relex_only or readiness"`
  → **95 passed, 1 skipped** (includes `test_relex_only_runtime_invariant.py`).

## Fixture reruns (pre-repair)

`data_eval/book_ingestion_pipeline_probe.json` (2026-08-03):

| check | state |
|---|---|
| permanent_fixtures_present | passed |
| fixtures_relex_only_identity | passed |
| fixtures_have_parent_and_document_summaries | **failed** (0/17 parents, 0/2 profiles) |
| fixtures_have_query_ready_certificates | **failed** (runs reconciling) |
| mixed_book_kinds_complete | passed |
| mixed_book_neo4j_bundle | passed |
| retrieval_returns_exact_chunk_ids_after_restart | passed |
| no_noncanonical_corpus_engine_values | **failed** (runpod_flash 16, cloud 1) |

Root blocker (from `stage_attempts`): `SummaryCostAuthorityRequired:
summary_cost_run_id is required for provider-backed summaries` at
`run_bounded_corpus_repair_cycle` attempt_no=31 on fixture corpus `f842e3b5`.

## Data-evaluation artifacts preserved

- `data_eval/book_ingestion_pipeline_probe.json`
- `data_eval/book_ingestion_pressure_baseline.json`
- `data_eval/book_pipeline_repair_baseline.json` (this snapshot)
- `data_eval/mixed_content_book_slice1_report.md`

No percentages used anywhere in this baseline.

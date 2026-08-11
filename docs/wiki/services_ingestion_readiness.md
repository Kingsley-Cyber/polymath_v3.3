# readiness

Source `backend/services/ingestion/readiness.py` (2291 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Computed corpus-readiness contract.

Synthesis: imported library module; first docstring sentence: “Computed corpus-readiness contract.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `neo4j_pressure_from_graph_promotion_jobs` | 204 | `graph_jobs: dict[str, int] \| None, recent_jobs: list[dict[str, Any]] \| None, *, now: datetime \| None=None, max_sam...` |
| `build_corpus_readiness_record` | 313 | `snapshot: dict[str, Any], *, computed_at: str \| None=None, stale: bool=False, refresh_error: str \| None=None` |
| `build_corpus_readiness_snapshot` | 334 | `*, corpus_id: str, document_counts: dict[str, Any] \| None=None, stage_counts: dict[str, int] \| None=None, chunk_cou...` |
| `compute_corpus_readiness` | 1109 | `db: Any, corpus_id: str` |
| `get_materialized_corpus_readiness` | 2257 | `db: Any, corpus_id: str` |
| `materialize_corpus_readiness` | 2264 | `db: Any, corpus_id: str, *, snapshot: dict[str, Any] \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `datetime`, `typing`, `config`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/routers/ingestion.py`, `backend/services/ingestion/corpus_commander.py`, `backend/services/ingestion/corpus_repair.py`, `backend/services/ingestion/route_readiness.py`, `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_corpus_readiness.py`, `backend/tests/test_q9_parse_policy.py`, `backend/tests/test_summary_readiness_baseline.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `corpora`, `document_pipeline_jobs`, `documents`, `extraction_jobs`, `ghost_b_extractions`, `graph_promotion_jobs`, `ingest_repair_runs`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_corpus_readiness.py`, `backend/tests/test_q9_parse_policy.py`, `backend/tests/test_summary_readiness_baseline.py`
- Size 2291 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (2291 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- Audited in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) sections: “2. Batch/buffer size knobs”, “4. Backpressure / memory-aware guards” — link into the map, do not duplicate it.
- Defect-fix commits: 1 — e.g. “9b1f528 fix: harden and optimize durable ingestion”. Full list: `git log --all --oneline -- backend/services/ingestion/readiness.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

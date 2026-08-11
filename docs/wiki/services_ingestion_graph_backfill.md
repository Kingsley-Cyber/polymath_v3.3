# graph_backfill

Source `backend/services/ingestion/graph_backfill.py` (890 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Retry failed Ghost B graph extraction chunks after a document lands.

Synthesis: imported library module; first docstring sentence: “Retry failed Ghost B graph extraction chunks after a document lands.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `backfill_failed_graph_chunks` | 415 | `*, db: AsyncIOMotorDatabase, qdrant_client: AsyncQdrantClient, neo4j_driver: Any, corpus_id: str, doc_id: str, user_i...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `dataclasses`, `datetime`, `typing`, `motor`, `qdrant_client`, `models`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/run_graphify_fixture_e2e.py`, `backend/services/graph/assertion_projector.py`, `backend/services/graph/projection_runner.py`, `backend/services/ingestion/extraction_jobs.py`, `backend/services/ingestion/graph_promotion_jobs.py`, `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_ingest_batches.py`, `backend/tests/test_release_registry_loader.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `corpora`, `documents`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_ingest_batches.py`, `backend/tests/test_release_registry_loader.py`
- Size 890 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (890 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 2 — e.g. “deaefc0 fix: backfill path honors ghost_b_llm — completes the seam for dead-last enrichment”. Full list: `git log --all --oneline -- backend/services/ingestion/graph_backfill.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

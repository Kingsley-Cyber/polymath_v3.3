# verify

Source `backend/services/ingestion/verify.py` (627 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Phase E — post-write verification.

Synthesis: imported library module; first docstring sentence: “Phase E — post-write verification.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `stamp_by_design_vector_omissions` | 61 | `db: AsyncIOMotorDatabase, *, doc_id: str, corpus_id: str` |
| `expected_summary_points_from_state` | 90 | `write_state: Any` |
| `verify_ingest` | 338 | `*, db: AsyncIOMotorDatabase, qdrant: AsyncQdrantClient, neo4j_driver: Any \| None, doc_id: str, corpus_id: str, targe...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `logging`, `typing`, `motor`, `qdrant_client`, `qdrant_client`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/reconcile_verified_documents.py`, `backend/services/ingestion/document_pipeline_executors.py`, `backend/services/ingestion/worker.py`
- **Tests**: `backend/tests/test_ingest_verify_graph_indexes.py`, `backend/tests/test_vector_omission_conservation.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `documents`, `parent_chunks`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_ingest_verify_graph_indexes.py`, `backend/tests/test_vector_omission_conservation.py`
- Size 627 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (627 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 3 — e.g. “82b37ff O2 crash battery closed + O4 vector-omission conservation + honest e2e gates”. Full list: `git log --all --oneline -- backend/services/ingestion/verify.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

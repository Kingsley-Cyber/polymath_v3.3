# document_pipeline_executors

Source `backend/services/ingestion/document_pipeline_executors.py` (658 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Executors for durable document-stage repair jobs.

Synthesis: imported library module; first docstring sentence: “Executors for durable document-stage repair jobs.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `mark_documents_persisted_from_artifacts` | 236 | `db: Any, *, corpus_id: str, doc_ids: list[str], limit: int=25` |
| `embed_documents_to_qdrant_from_artifacts` | 292 | `db: Any, *, qdrant_client: Any, neo4j_driver: Any \| None=None, corpus_id: str, doc_ids: list[str], limit: int=10` |
| `index_document_summaries_from_artifacts` | 512 | `db: Any, *, qdrant_client: Any, neo4j_driver: Any \| None=None, corpus_id: str, doc_ids: list[str], limit: int=10` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `time`, `datetime`, `typing`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/run_graphify_fixture_e2e.py`, `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_document_pipeline_jobs.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `corpora`, `documents`, `parent_chunks`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_document_pipeline_jobs.py`
- Size 658 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (658 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 4 — e.g. “82b37ff O2 crash battery closed + O4 vector-omission conservation + honest e2e gates”. Full list: `git log --all --oneline -- backend/services/ingestion/document_pipeline_executors.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

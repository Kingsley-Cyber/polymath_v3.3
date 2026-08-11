# worker

Source `backend/services/ingestion/worker.py` (5103 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Ingestion pipeline worker — locked pipeline order:

Synthesis: imported library module; first docstring sentence: “Ingestion pipeline worker — locked pipeline order:”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run_ingest_job` | 3360 | `job_id: str, data: bytes, filename: str, corpus_id: str, user_id: str, ingestion_config: IngestionConfig, db: AsyncIO...` |
| `AdjustableSemaphore.set_limit` | 434 | `self, limit: int` |

## 3. Dependencies

- **Imports IN** (first-party stems): `asyncio`, `functools`, `collections`, `hashlib`, `inspect`, `logging`, `mimetypes`, `os`, `re`, `time`, `uuid`, `datetime`, `typing`, `config`, `models`, `motor`, `qdrant_client`, `services`, `services`, `dataclasses`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/materialize_gsem_fixture_retrieval.py`, `backend/scripts/run_graphify_fixture_e2e.py`, `backend/services/ingestion/document_pipeline_executors.py`, `backend/services/ingestion/graph_backfill.py`, `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/extraction/test_graphify_entrypoint.py`, `backend/tests/test_code_graph_synthesis.py`, `backend/tests/test_facet_schema.py`, `backend/tests/test_ghost_b_staging.py`, `backend/tests/test_provider_lane_health.py`, `backend/tests/test_searchable_text.py` …
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `corpora`, `documents`, `ghost_b_error_events`, `ghost_b_extractions`, `parent_chunks`
- **Env vars** (name → default): `CHUNK_REMOTE_URLS`→``

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_graphify_entrypoint.py`, `backend/tests/test_code_graph_synthesis.py`, `backend/tests/test_facet_schema.py`, `backend/tests/test_ghost_b_staging.py`, `backend/tests/test_provider_lane_health.py`, `backend/tests/test_searchable_text.py`, `backend/tests/test_symbols_called_backfill.py`, `backend/tests/test_worker_phases.py`, `backend/tests/test_worker_summary_control_plane.py`
- Size 5103 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (5103 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- Audited in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) sections: “2. Batch/buffer size knobs”, “3. Subprocess memory: OpenIE farm and chunk process pools”, “6. Per-collection write amplification for one document ingest” — link into the map, do not duplicate it.
- Defect-fix commits: 8 — e.g. “82b37ff O2 crash battery closed + O4 vector-omission conservation + honest e2e gates”. Full list: `git log --all --oneline -- backend/services/ingestion/worker.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

## VERIFY

Drift check — grep the repo and confirm these still hold (run `docs/wiki/verify_claims.py`):

- `chunk subprocess imports only tier_chunker` → backend/services/ingestion/chunk_subprocess.py:1-5
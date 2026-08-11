# document_pipeline_jobs

Source `backend/services/ingestion/document_pipeline_jobs.py` (984 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Durable document-stage ingestion job planner.

Synthesis: imported library module; first docstring sentence: “Durable document-stage ingestion job planner.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `reconcile_satisfied_document_pipeline_jobs` | 111 | `db: Any, *, corpus_id: str, limit: int=5000` |
| `document_pipeline_contract` | 182 | `doc: dict[str, Any] \| None` |
| `document_pipeline_contract_hash` | 196 | `doc: dict[str, Any] \| None` |
| `document_source_fingerprint` | 200 | `doc: dict[str, Any] \| None` |
| `document_pipeline_job_id` | 224 | `*, corpus_id: str, doc_id: str, kind: str, source_fingerprint: str, contract_hash: str` |
| `build_document_pipeline_job` | 249 | `*, doc: dict[str, Any], kind: str, child_chunks: int, parent_chunks: int` |
| `classify_document_pipeline_jobs` | 349 | `*, doc: dict[str, Any], child_chunks: int, parent_chunks: int` |
| `plan_document_pipeline_jobs` | 611 | `db: Any, *, corpus_id: str, user_id: str \| None=None, apply: bool=False, limit: int=500, kinds: list[str] \| None=None` |
| `run_document_pipeline_jobs` | 744 | `db: Any, *, corpus_id: str, user_id: str \| None=None, limit: int=25, statuses: list[str] \| None=None, kinds: list[s...` |
| `list_document_pipeline_jobs` | 953 | `db: Any, *, corpus_id: str, limit: int=100, statuses: list[str] \| None=None, kinds: list[str] \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `datetime`, `typing`, `pymongo`, `db`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/services/control_plane/reconciler.py`, `backend/services/ingestion/corpus_commander.py`, `backend/services/ingestion/corpus_repair.py`, `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_control_plane_v2.py`, `backend/tests/test_document_pipeline_jobs.py`, `backend/tests/test_q9_parse_policy.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `document_pipeline_jobs`, `documents`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_control_plane_v2.py`, `backend/tests/test_document_pipeline_jobs.py`, `backend/tests/test_q9_parse_policy.py`
- Size 984 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (984 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 2 — e.g. “3437b9c fix: stop redundant vector repair cycles”. Full list: `git log --all --oneline -- backend/services/ingestion/document_pipeline_jobs.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

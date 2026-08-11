# source_parse_jobs

Source `backend/services/ingestion/source_parse_jobs.py` (815 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Durable source/parse job read model.

Synthesis: imported library module; first docstring sentence: “Durable source/parse job read model.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `source_parse_contract` | 92 | `batch: dict[str, Any] \| None` |
| `source_parse_contract_hash` | 107 | `batch: dict[str, Any] \| None` |
| `source_parse_job_id` | 111 | `*, corpus_id: str, batch_id: str, item_id: str, source_fingerprint: str, contract_hash: str` |
| `source_fingerprint` | 127 | `item: dict[str, Any]` |
| `classify_source_parse_status` | 163 | `item: dict[str, Any]` |
| `build_source_parse_job` | 189 | `*, item: dict[str, Any], batch: dict[str, Any] \| None` |
| `plan_source_parse_jobs` | 274 | `db: Any, *, corpus_id: str, user_id: str \| None=None, apply: bool=False, limit: int=500` |
| `backfill_source_parse_stage_identity` | 390 | `db: Any, *, corpus_id: str, user_id: str \| None=None, apply: bool=False, limit: int=1000` |
| `list_source_parse_jobs` | 627 | `db: Any, *, corpus_id: str, limit: int=100, statuses: list[str] \| None=None` |
| `run_source_parse_jobs` | 653 | `db: Any, *, corpus_id: str, user_id: str, ingestion_service: Any \| None=None, limit: int=25, statuses: list[str] \| ...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `datetime`, `pathlib`, `typing`, `pymongo`, `db`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/services/control_plane/reconciler.py`, `backend/services/ingestion/corpus_commander.py`, `backend/services/ingestion/corpus_repair.py`, `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_source_parse_jobs.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `ingest_batch_items`, `ingest_batches`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_source_parse_jobs.py`
- Size 815 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (815 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

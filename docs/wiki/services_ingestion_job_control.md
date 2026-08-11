# job_control

Source `backend/services/ingestion/job_control.py` (155 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Inspectable operator controls for durable ingestion jobs.

Synthesis: imported library module; first docstring sentence: “Inspectable operator controls for durable ingestion jobs.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `collection_for_lane` | 20 | `lane: str` |
| `list_jobs` | 27 | `db: Any, *, corpus_id: str, lane: str \| None=None, statuses: list[str] \| None=None, limit: int=100` |
| `control_job` | 69 | `db: Any, *, corpus_id: str, lane: str, job_id: str, action: str, reason: str, operator_user_id: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `datetime`, `typing`, `services`
- **Imports OUT** (repo-wide): `backend/routers/ingestion.py`
- **Tests**: `backend/tests/test_job_control.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `ingest_scheduler_state`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_job_control.py`
- Size 155 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 2 — e.g. “679cd46 fix: wake scheduler after operator retry”. Full list: `git log --all --oneline -- backend/services/ingestion/job_control.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

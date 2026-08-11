# organ_repair_jobs

Source `backend/services/ingestion/organ_repair_jobs.py` (280 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Organ repair as a first-class, reconciler-owned lane.

Synthesis: imported library module; first docstring sentence: “Organ repair as a first-class, reconciler-owned lane.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `organ_repair_job_id` | 55 | `*, corpus_id: str, organ: str` |
| `plan_organ_repair_jobs` | 61 | `db: Any, *, corpus_id: str, user_id: str \| None=None, organ_gaps: dict[str, dict[str, Any]] \| None=None, apply: boo...` |
| `run_organ_repair_job` | 240 | `db: Any, job: dict[str, Any]` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `time`, `datetime`, `typing`, `services`
- **Imports OUT** (repo-wide): `backend/services/control_plane/reconciler.py`
- **Tests**: `backend/tests/test_organ_repair_lane.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `ghost_b_extractions`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_organ_repair_lane.py`
- Size 280 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

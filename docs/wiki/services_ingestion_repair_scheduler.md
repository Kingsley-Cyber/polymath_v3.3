# repair_scheduler

Source `backend/services/ingestion/repair_scheduler.py` (197 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Cheap, readiness-driven scheduling state for corpus repair.

Synthesis: imported library module; first docstring sentence: “Cheap, readiness-driven scheduling state for corpus repair.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `quick_repair_gap_snapshot` | 61 | `db: Any, corpus_id: str` |
| `backoff_decision` | 127 | `*, snapshot: dict[str, Any], state: dict[str, Any] \| None, now: datetime` |
| `load_scheduler_state` | 148 | `db: Any, corpus_id: str` |
| `record_scheduler_outcome` | 158 | `db: Any, *, corpus_id: str, snapshot: dict[str, Any], changed: bool, now: datetime \| None=None, base_seconds: int=12...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `hashlib`, `json`, `datetime`, `typing`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_repair_scheduler.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `documents`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_repair_scheduler.py`
- Size 197 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “9b1f528 fix: harden and optimize durable ingestion”. Full list: `git log --all --oneline -- backend/services/ingestion/repair_scheduler.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

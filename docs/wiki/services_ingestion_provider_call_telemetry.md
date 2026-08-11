# provider_call_telemetry

Source `backend/services/ingestion/provider_call_telemetry.py` (202 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Secret-free provider call accounting for ingestion phases.

Synthesis: imported library module; first docstring sentence: “Secret-free provider call accounting for ingestion phases.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `provider_family` | 31 | `value: Any` |
| `record_provider_call` | 44 | `db: Any, event: dict[str, Any]` |
| `provider_efficiency_snapshot` | 106 | `db: Any, *, corpus_id: str, window_hours: int=24` |
| `record_ghost_b_event` | 156 | `db: Any, event: dict[str, Any]` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `datetime`, `typing`
- **Imports OUT** (repo-wide): `backend/services/ingestion/readiness.py`, `backend/services/ingestion/summary_backfill.py`, `backend/services/ingestion/worker.py`, `backend/services/ingestion_service.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 202 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “9b1f528 fix: harden and optimize durable ingestion”. Full list: `git log --all --oneline -- backend/services/ingestion/provider_call_telemetry.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

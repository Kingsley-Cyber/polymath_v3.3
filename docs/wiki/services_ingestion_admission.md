# admission

Source `backend/services/ingestion/admission.py` (104 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Process-local ingest admission control.

Synthesis: imported library module; first docstring sentence: “Process-local ingest admission control.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `try_acquire_ingest_slot` | 58 | `limit: int \| None=None` |
| `release_ingest_slot` | 79 | `()` |
| `active_count` | 92 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `logging`, `config`
- **Imports OUT** (repo-wide): `backend/polymath_mcp/tools.py`, `backend/routers/ingestion.py`, `backend/services/ingestion/batches.py`
- **Tests**: `backend/tests/test_ingest_slot_ordering.py`, `backend/tests/test_mcp_ingest_slot_gate.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_ingest_slot_ordering.py`, `backend/tests/test_mcp_ingest_slot_gate.py`
- Size 104 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

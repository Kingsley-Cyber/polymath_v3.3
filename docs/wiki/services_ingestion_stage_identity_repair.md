# stage_identity_repair

Source `backend/services/ingestion/stage_identity_repair.py` (248 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Bounded stage-identity repair helpers.

Synthesis: imported library module; first docstring sentence: “Bounded stage-identity repair helpers.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `build_ghost_b_stage_identity_update` | 104 | `row: dict[str, Any], *, doc: dict[str, Any], chunk: dict[str, Any], contract_hash: str, now: datetime` |
| `backfill_ghost_b_stage_identity` | 133 | `db: Any, *, corpus_id: str, apply: bool=False, limit: int=1000, statuses: list[str] \| tuple[str, ...] \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `collections`, `datetime`, `typing`, `pymongo`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion/corpus_repair.py`
- **Tests**: `backend/tests/test_stage_identity_repair.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `documents`, `ghost_b_extractions`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_stage_identity_repair.py`
- Size 248 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

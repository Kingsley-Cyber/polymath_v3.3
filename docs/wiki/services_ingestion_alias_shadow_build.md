# alias_shadow_build

Source `backend/services/ingestion/alias_shadow_build.py` (173 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Production ingest → alias SHADOW builder (owner-ordered wiring, 2026-08-09).

Synthesis: imported library module; first docstring sentence: “Production ingest → alias SHADOW builder (owner-ordered wiring, 2026-08-09).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `rebuild_corpus_alias_shadow` | 42 | `db: Any, *, corpus_id: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `datetime`, `typing`, `models`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion/batches.py`
- **Tests**: `backend/tests/test_alias_shadow_build.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `ghost_b_extractions`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_alias_shadow_build.py`
- Size 173 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

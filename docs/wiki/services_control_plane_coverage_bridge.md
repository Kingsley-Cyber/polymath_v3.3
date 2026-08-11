# coverage_bridge

Source `backend/services/control_plane/coverage_bridge.py` (110 lines) · subsystem [control-plane](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Turn measured organ coverage into LANE-AWARE gaps the reconciler can act on.

Synthesis: imported library module; first docstring sentence: “Turn measured organ coverage into LANE-AWARE gaps the reconciler can act on.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `corpus_organ_gaps` | 34 | `db: Any, *, corpus_id: str` |
| `unowned_organ_gaps` | 80 | `db: Any, *, corpus_id: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `typing`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/services/control_plane/reconciler.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `ghost_b_extractions`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 110 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

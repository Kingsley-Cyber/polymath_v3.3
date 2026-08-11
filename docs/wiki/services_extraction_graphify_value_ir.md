# graphify_value_ir

Source `backend/services/extraction/graphify_value_ir.py` (98 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Typed value + temporal qualifier IR (#7, owner-ratified 2026-08-07).

Synthesis: imported library module; first docstring sentence: “Typed value + temporal qualifier IR (#7, owner-ratified 2026-08-07).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `parse_typed_value` | 62 | `surface: str` |
| `extract_temporal_qualifier` | 83 | `evidence_text: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`
- **Imports OUT** (repo-wide): `backend/services/extraction/graphify_assertion_assembler.py`, `backend/services/extraction/graphify_proposition_reducer.py`, `backend/services/extraction/graphify_relations.py`
- **Tests**: `backend/tests/extraction/test_graphify_value_ir.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_graphify_value_ir.py`
- Size 98 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

# config_locator

Source `backend/services/extraction/config_locator.py` (24 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Locate the versioned config/ directory from any deployment layout.

Synthesis: imported library module; first docstring sentence: “Locate the versioned config/ directory from any deployment layout.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `find_config_dir` | 15 | `anchor: str \| Path` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `pathlib`
- **Imports OUT** (repo-wide): `backend/services/extraction/canonical.py`, `backend/services/extraction/gliner2_cpu_provider.py`, `backend/services/extraction/graphify_relations.py`, `backend/services/ontology_adapter/schema_compiler.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 24 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

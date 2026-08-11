# document_semantic_profile

Source `backend/services/ingestion/document_semantic_profile.py` (672 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic T9.1 document-profile compiler.

Synthesis: imported library module; first docstring sentence: “Deterministic T9.1 document-profile compiler.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `current_registry_closure` | 51 | `()` |
| `current_profile_recipe_hash` | 73 | `closure: ProfileRegistryClosureV1 \| None=None` |
| `compile_document_profile` | 313 | `*, document: Mapping[str, Any], parent_rows: Iterable[Mapping[str, Any]], extraction_rows: Iterable[Mapping[str, Any]]` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `collections`, `collections`, `typing`, `models`, `models`, `models`, `models`, `models`, `models`, `models`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/materialize_t91_document_profiles.py`, `backend/services/retriever/four_lane_router.py`
- **Tests**: `backend/tests/test_document_semantic_profile.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_document_semantic_profile.py`
- Size 672 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (672 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

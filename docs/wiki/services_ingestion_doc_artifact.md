# doc_artifact

Source `backend/services/ingestion/doc_artifact.py` (373 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Passive document artifact compiler for source-role synthesis headers.

Synthesis: imported library module; first docstring sentence: “Passive document artifact compiler for source-role synthesis headers.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `build_doc_artifact` | 236 | `doc_profile: dict[str, Any] \| None, facet_profile: dict[str, Any] \| None=None, source_meta: dict[str, Any] \| None=...` |
| `format_source_role_header` | 354 | `doc_label: str, artifact: dict[str, Any] \| None` |
| `DocArtifact.to_dict` | 61 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `collections`, `dataclasses`, `typing`
- **Imports OUT** (repo-wide): `backend/scripts/backfill_doc_artifacts.py`, `backend/services/context_manager.py`, `backend/services/ingestion/summary_tree.py`, `backend/services/retriever/assembly.py`
- **Tests**: `backend/tests/test_doc_artifact.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_doc_artifact.py`
- Size 373 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

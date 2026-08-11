# knowledge_bundle

Source `backend/services/ingestion/knowledge_bundle.py` (293 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Adapt live ExtractionResult / ghost_b rows → KnowledgeArtifactBundleV1.

Synthesis: imported library module; first docstring sentence: “Adapt live ExtractionResult / ghost_b rows → KnowledgeArtifactBundleV1.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `bundle_from_extraction_row` | 85 | `row: Any, *, corpus_generation: str='', parent_id: str='', section_id: str='', run_id: str='', heading_path: list[str...` |
| `bundles_for_document` | 237 | `db: Any, *, corpus_id: str, document_id: str, corpus_generation: str=''` |
| `assertion_rows_from_bundle` | 271 | `bundle: KnowledgeArtifactBundleV1` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `typing`, `models`
- **Imports OUT** (repo-wide): `backend/services/graph/assertion_projector.py`, `backend/services/ingestion/fixture_knowledge_pipeline.py`
- **Tests**: `backend/tests/test_knowledge_artifact_bundle.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `ghost_b_extractions`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_knowledge_artifact_bundle.py`
- Size 293 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

# alias_schema_projection

Source `backend/services/ingestion/alias_schema_projection.py` (249 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Project CorpusEntityV1 into separated shadow schema trust classes (Phase 7).

Synthesis: imported library module; first docstring sentence: “Project CorpusEntityV1 into separated shadow schema trust classes (Phase 7).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `project_corpus_entity_to_shadow_schema` | 63 | `entity: CorpusEntityV1, *, links: SchemaProjectionLinks \| None=None, legacy_query_aliases: Sequence[str]=(), alias_c...` |
| `project_corpus_entities_to_shadow_schemas` | 217 | `entities: Iterable[CorpusEntityV1] \| None, *, links_by_entity_id: dict[str, SchemaProjectionLinks] \| None=None, leg...` |
| `shadow_records_as_dicts` | 247 | `batch: SchemaProjectionBatch` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `dataclasses`, `typing`, `models`, `models`
- **Imports OUT** (repo-wide): `backend/services/ingestion/alias_shadow_build.py`, `backend/services/ingestion/fixture_knowledge_pipeline.py`
- **Tests**: `backend/tests/test_alias_retrieval_shadow_phase8.py`, `backend/tests/test_alias_schema_retrieval_phase7.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_alias_retrieval_shadow_phase8.py`, `backend/tests/test_alias_schema_retrieval_phase7.py`
- Size 249 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

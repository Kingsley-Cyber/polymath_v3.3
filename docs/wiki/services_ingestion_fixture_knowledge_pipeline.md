# fixture_knowledge_pipeline

Source `backend/services/ingestion/fixture_knowledge_pipeline.py` (605 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Historical isolated fixture pipeline for alias identity lineage.

Synthesis: imported library module; first docstring sentence: “Historical isolated fixture pipeline for alias identity lineage.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `assert_fixture_safe` | 67 | `corpus: dict[str, Any]` |
| `load_fixture_docs` | 127 | `source_dir: Path` |
| `ensure_fixture_corpus` | 154 | `db: Any, *, corpus_id: str` |
| `enrich_bundle_offsets` | 243 | `bundle: KnowledgeArtifactBundleV1, text: str` |
| `run_phase1_fixture_pipeline` | 302 | `db: Any, *, corpus_id: str, source_dir: Path, neo4j_driver: Any \| None=None` |
| `Phase1Proof.ok` | 107 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `logging`, `re`, `dataclasses`, `datetime`, `pathlib`, `typing`, `models`, `models`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/materialize_gsem_fixture_retrieval.py`, `backend/scripts/run_candidate_adoption_suite.py`, `backend/scripts/run_complex_query_dark_canary.py`, `backend/scripts/run_complex_query_fixture_e2e.py`, `backend/scripts/run_complex_query_generation_probes.py`, `backend/scripts/run_complex_query_validation_delta.py`, `backend/scripts/run_expanded_dark_canary.py`, `backend/scripts/run_graph_semantic_e2e_phase2_plus.py`, `backend/scripts/run_graph_semantic_e2e_phase7_10.py`, `backend/services/ingestion/alias_shadow_build.py`
- **Tests**: `backend/tests/test_alias_shadow_build.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `corpora`, `documents`, `ghost_b_extractions`, `graph_projection_jobs`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_alias_shadow_build.py`
- Size 605 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (605 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

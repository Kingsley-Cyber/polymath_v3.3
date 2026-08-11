# graphify_survey

Source `backend/services/extraction/graphify_survey.py` (263 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic, zero-model survey for Graphify source documents.

Synthesis: imported library module; first docstring sentence: “Deterministic, zero-model survey for Graphify source documents.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `survey_document` | 126 | `document: NormalizedDocumentV1` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `re`, `collections`, `typing`, `pydantic`, `models`
- **Imports OUT** (repo-wide): `backend/scripts/ab_gliner2_device.py`, `backend/scripts/bench_entity_encoders.py`, `backend/scripts/replay_downstream.py`, `backend/scripts/report_graphify_s04.py`, `backend/scripts/run_graphify_census.py`, `backend/scripts/run_graphify_reducer.py`, `backend/scripts/run_graphify_relations.py`, `backend/scripts/run_relation_eligibility.py`, `backend/services/extraction/graphify_census.py`, `backend/services/extraction/graphify_pipeline.py` …
- **Tests**: `backend/tests/extraction/test_graphify_census.py`, `backend/tests/extraction/test_graphify_contracts_offsets_survey.py`, `backend/tests/extraction/test_graphify_endpoint_eligibility.py`, `backend/tests/extraction/test_graphify_reducer.py`, `backend/tests/extraction/test_graphify_relations.py`, `backend/tests/extraction/test_graphify_unit_kind.py` …

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_graphify_census.py`, `backend/tests/extraction/test_graphify_contracts_offsets_survey.py`, `backend/tests/extraction/test_graphify_endpoint_eligibility.py`, `backend/tests/extraction/test_graphify_reducer.py`, `backend/tests/extraction/test_graphify_relations.py`, `backend/tests/extraction/test_graphify_unit_kind.py`, `backend/tests/extraction/test_relation_eligibility_artifact.py`
- Size 263 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

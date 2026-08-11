# graphify_normalization

Source `backend/services/extraction/graphify_normalization.py` (70 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Loss-aware source normalization with reversible character boundaries.

Synthesis: imported library module; first docstring sentence: “Loss-aware source normalization with reversible character boundaries.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `normalize_document` | 25 | `document_id: str, text: str, source_uri: str=''` |
| `to_original_span` | 54 | `document: NormalizedDocumentV1, start: int, end: int` |
| `assert_round_trip` | 64 | `document: NormalizedDocumentV1, start: int, end: int` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `unicodedata`, `dataclasses`, `models`
- **Imports OUT** (repo-wide): `backend/scripts/bench_entity_encoders.py`, `backend/scripts/report_graphify_s04.py`, `backend/scripts/run_graphify_census.py`, `backend/scripts/run_graphify_completion.py`, `backend/scripts/run_graphify_openie.py`, `backend/scripts/run_graphify_reducer.py`, `backend/scripts/run_graphify_relations.py`, `backend/scripts/run_relation_eligibility.py`, `backend/services/extraction/graphify_census.py`, `backend/services/extraction/graphify_completion.py` …
- **Tests**: `backend/tests/extraction/test_graphify_census.py`, `backend/tests/extraction/test_graphify_completion.py`, `backend/tests/extraction/test_graphify_contracts_offsets_survey.py`, `backend/tests/extraction/test_graphify_endpoint_eligibility.py`, `backend/tests/extraction/test_graphify_openie.py`, `backend/tests/extraction/test_graphify_reducer.py` …

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_graphify_census.py`, `backend/tests/extraction/test_graphify_completion.py`, `backend/tests/extraction/test_graphify_contracts_offsets_survey.py`, `backend/tests/extraction/test_graphify_endpoint_eligibility.py`, `backend/tests/extraction/test_graphify_openie.py`, `backend/tests/extraction/test_graphify_reducer.py`, `backend/tests/extraction/test_graphify_relations.py`, `backend/tests/extraction/test_graphify_unit_kind.py`, `backend/tests/extraction/test_relation_eligibility_artifact.py`
- Size 70 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

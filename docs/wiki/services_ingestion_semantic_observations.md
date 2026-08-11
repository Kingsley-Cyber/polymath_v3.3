# semantic_observations

Source `backend/services/ingestion/semantic_observations.py` (808 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic spaCy observation capture and claim-candidate compilation.

Synthesis: imported library module; first docstring sentence: “Deterministic spaCy observation capture and claim-candidate compilation.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `build_spacy_observation_bundle` | 290 | `*, text: str, nlp: Any, source_version_id: str, hierarchy_node_id: str, parser_id: str, parser_version: str` |
| `semantic_observation_recipe_hash` | 506 | `*, parser_id: str, parser_version: str` |
| `compile_local_extraction_v1` | 547 | `bundle: ObservationBundle, *, document_id: str, child_id: str` |
| `load_normalization_identity` | 638 | `()` |
| `local_extraction_recipe_hash` | 649 | `()` |
| `compile_claim_candidates` | 688 | `bundle: ObservationBundle` |
| `validate_evidence_round_trip` | 791 | `bundle: ObservationBundle, text: str` |
| `normalized_cue` | 806 | `value: str` |
| `LocalExtractionCompileResult.receipt` | 117 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `collections`, `dataclasses`, `re`, `typing`, `models`, `models`, `models`, `models`
- **Imports OUT** (repo-wide): `backend/scripts/audit_claim_assessment_ugo.py`, `backend/scripts/audit_claim_compiler_ugo.py`, `backend/scripts/audit_local_extraction_ugo.py`, `backend/services/ingestion/claim_compiler.py`, `backend/services/ingestion/semantic_digest_claim_inputs.py`
- **Tests**: `backend/tests/test_atomic_claim_anchors.py`, `backend/tests/test_claim_compiler.py`, `backend/tests/test_local_extraction.py`, `backend/tests/test_semantic_digest_claim_inputs.py`, `backend/tests/test_semantic_observations.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_atomic_claim_anchors.py`, `backend/tests/test_claim_compiler.py`, `backend/tests/test_local_extraction.py`, `backend/tests/test_semantic_digest_claim_inputs.py`, `backend/tests/test_semantic_observations.py`
- Size 808 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (808 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

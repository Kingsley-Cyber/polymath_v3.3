# semantic_digest_claim_inputs

Source `backend/services/ingestion/semantic_digest_claim_inputs.py` (1400 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Compile, validate, materialize, and packetize deterministic atomic claims.

Synthesis: imported library module; first docstring sentence: “Compile, validate, materialize, and packetize deterministic atomic claims.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `load_bounded_selection_recipe` | 184 | `()` |
| `document_source_version_id` | 202 | `document: Mapping[str, Any]` |
| `compile_child_candidate` | 228 | `*, corpus_id: str, document: Mapping[str, Any], child: Mapping[str, Any], nlp: Any, spacy_library_version: str` |
| `compile_existing_child_candidate` | 293 | `*, corpus_id: str, document: Mapping[str, Any], child: Mapping[str, Any], extraction_row: Mapping[str, Any], nlp: Any...` |
| `validate_candidate_against_source` | 405 | `candidate: CompiledChildCandidateExportV1, *, corpus_id: str, document: Mapping[str, Any], child: Mapping[str, Any]` |
| `materialize_candidate_row` | 466 | `candidate: CompiledChildCandidateExportV1, *, corpus_id: str, document: Mapping[str, Any], child: Mapping[str, Any], ...` |
| `validate_materialized_row_against_source` | 607 | `row: ClaimCompilationMaterializationRowV1, *, corpus_id: str, document: Mapping[str, Any], child: Mapping[str, Any]` |
| `build_atomic_parent_packet` | 670 | `*, corpus_id: str, corpus_name: str, parent: Mapping[str, Any], compilation_rows: Mapping[str, ClaimCompilationMateri...` |
| `build_sentence_hybrid_parent_packet` | 844 | `*, corpus_id: str, corpus_name: str, parent: Mapping[str, Any], compilation_rows: Mapping[str, ClaimCompilationMateri...` |
| `expand_sentence_claim_ids` | 1005 | `build: SentenceHybridParentPacketBuild, sentence_claim_ids: Sequence[str], *, expected_parent_id: str, expected_sourc...` |
| `build_bounded_atomic_parent_packet` | 1254 | `*, corpus_id: str, corpus_name: str, parent: Mapping[str, Any], compilation_rows: Mapping[str, ClaimCompilationMateri...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `dataclasses`, `datetime`, `json`, `pathlib`, `re`, `typing`, `models`, `models`, `models`, `models`, `models`, `models`, `models`, `models`, `models`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/diagnose_atomic_claim_anchor_coverage.py`, `backend/scripts/materialize_semantic_digest_claim_inputs.py`, `backend/scripts/semantic_gateway_mark_atomic_preflight.py`, `backend/scripts/semantic_gateway_mark_sentence_hybrid_preflight.py`, `backend/services/ingestion/document_semantic_profile.py`, `backend/services/retriever/atomic_claim_anchors.py`, `backend/services/retriever/four_lane_router.py`
- **Tests**: `backend/tests/test_atomic_claim_anchors.py`, `backend/tests/test_semantic_digest_claim_inputs.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_atomic_claim_anchors.py`, `backend/tests/test_semantic_digest_claim_inputs.py`
- Size 1400 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (1400 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

# claim_compiler

Source `backend/services/ingestion/claim_compiler.py` (768 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic ObservationBundle + LocalExtractionV1 claim compiler.

Synthesis: imported library module; first docstring sentence: “Deterministic ObservationBundle + LocalExtractionV1 claim compiler.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `project_claim_record_to_assertion` | 473 | `record: ClaimRecordV1` |
| `restore_claim_record_from_assertion` | 525 | `assertion: ClaimAssertionV1` |
| `compile_claim_records_v1` | 558 | `*, bundle: ObservationBundle, extraction: LocalExtractionV1` |
| `claim_compiler_recipe_hash` | 749 | `observation_recipe_hash: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `collections`, `re`, `typing`, `models`, `models`, `models`, `models`, `models`, `models`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/audit_claim_assessment_ugo.py`, `backend/scripts/audit_claim_compiler_ugo.py`, `backend/services/ingestion/semantic_digest_claim_inputs.py`
- **Tests**: `backend/tests/test_atomic_claim_anchors.py`, `backend/tests/test_claim_assessment.py`, `backend/tests/test_claim_compiler.py`, `backend/tests/test_semantic_digest_claim_inputs.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_atomic_claim_anchors.py`, `backend/tests/test_claim_assessment.py`, `backend/tests/test_claim_compiler.py`, `backend/tests/test_semantic_digest_claim_inputs.py`
- Size 768 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (768 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

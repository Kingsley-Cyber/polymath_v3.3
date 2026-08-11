# claim_assessment

Source `backend/services/ingestion/claim_assessment.py` (423 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic negation and typed-signature assessment for compiled claims.

Synthesis: imported library module; first docstring sentence: “Deterministic negation and typed-signature assessment for compiled claims.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `signature_contract_identity_v1` | 61 | `()` |
| `signature_contract_hash_v1` | 84 | `()` |
| `assess_claim_compilation_v1` | 261 | `*, bundle: ObservationBundle, extraction: LocalExtractionV1, compilation: ClaimCompilationV1, provenance: AssessmentP...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `collections`, `typing`, `models`, `models`, `models`, `models`, `models`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/audit_claim_assessment_ugo.py`
- **Tests**: `backend/tests/test_claim_assessment.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_claim_assessment.py`
- Size 423 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (423 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

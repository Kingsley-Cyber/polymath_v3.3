# relation_evidence

Source `backend/services/extraction/relation_evidence.py` (384 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Shared evidence contract for the relation corroboration gate.

Synthesis: imported library module; first docstring sentence: “Shared evidence contract for the relation corroboration gate.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `SyntaxEvidence.join_trust` | 200 | `self` |
| `RelationEvidence.relation_key` | 276 | `self` |
| `RelationEvidence.canonical_subject_id` | 287 | `self` |
| `RelationEvidence.canonical_object_id` | 298 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `dataclasses`, `enum`, `typing`
- **Imports OUT** (repo-wide): `backend/scripts/gate_diagnostic.py`, `backend/scripts/p1b_precision_audit.py`, `backend/scripts/run_gold_entity_syntax_ceiling.py`, `backend/scripts/shadow_mode_corroboration.py`, `backend/scripts/unified_shadow_pipeline.py`, `backend/services/extraction/corroboration_gate.py`, `backend/services/extraction/relex_adapter.py`, `backend/services/extraction/syntax_lane.py`
- **Tests**: `backend/tests/extraction/test_canonical_contract.py`, `backend/tests/extraction/test_corroboration_gate.py`, `backend/tests/extraction/test_relex_adapter.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_canonical_contract.py`, `backend/tests/extraction/test_corroboration_gate.py`, `backend/tests/extraction/test_relex_adapter.py`
- Size 384 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

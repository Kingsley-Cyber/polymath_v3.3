# extraction_organs

Source `backend/services/control_plane/extraction_organs.py` (144 lines) · subsystem [control-plane](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Accountable extraction-organ contract for the canonical Graphify lane.

Synthesis: imported library module; first docstring sentence: “Accountable extraction-organ contract for the canonical Graphify lane.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `organs_expected_for_lane` | 102 | `lane: str` |
| `lane_coverage_gaps` | 110 | `()` |
| `compile_organ_contract` | 120 | `lane: str` |
| `OrganSpec.to_doc` | 39 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `dataclasses`, `typing`
- **Imports OUT** (repo-wide): `backend/services/control_plane/coverage_bridge.py`, `backend/services/control_plane/desired_state.py`, `backend/services/ingestion/organ_repair_jobs.py`
- **Tests**: `backend/tests/test_extraction_organ_contract.py`, `backend/tests/test_organ_repair_lane.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_extraction_organ_contract.py`, `backend/tests/test_organ_repair_lane.py`
- Size 144 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

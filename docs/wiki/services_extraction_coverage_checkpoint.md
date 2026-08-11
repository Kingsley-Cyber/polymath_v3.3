# coverage_checkpoint

Source `backend/services/extraction/coverage_checkpoint.py` (175 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Extraction coverage checkpoints — make a dead organ impossible to miss.

Synthesis: imported library module; first docstring sentence: “Extraction coverage checkpoints — make a dead organ impossible to miss.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `classify` | 101 | `organ: str, value: float, total: int, chunks: int` |
| `build_pipeline` | 121 | `()` |
| `coverage_from_row` | 148 | `row: dict[str, Any], corpus_name: str=''` |
| `OrganCoverage.failed` | 68 | `self` |
| `CorpusCoverage.dead_organs` | 82 | `self` |
| `CorpusCoverage.failing_organs` | 86 | `self` |
| `CorpusCoverage.healthy` | 90 | `self` |
| `CorpusCoverage.to_doc` | 93 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `dataclasses`, `datetime`, `typing`
- **Imports OUT** (repo-wide): `backend/scripts/extraction_coverage_check.py`, `backend/services/control_plane/coverage_bridge.py`
- **Tests**: `backend/tests/test_extraction_coverage_checkpoint.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_extraction_coverage_checkpoint.py`
- Size 175 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

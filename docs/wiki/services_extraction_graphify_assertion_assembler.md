# graphify_assertion_assembler

Source `backend/services/extraction/graphify_assertion_assembler.py` (162 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Assemble evidence-scoped OpenIE candidates into explicit authority lanes.

Synthesis: imported library module; first docstring sentence: “Assemble evidence-scoped OpenIE candidates into explicit authority lanes.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `assemble_openie_assertions` | 34 | `candidates: Sequence[OpenIEPredicateCandidateV1], arguments: Sequence[AdaptedOpenIEArgumentV1]` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `collections`, `dataclasses`, `re`, `typing`, `services`, `models`
- **Imports OUT** (repo-wide): `backend/scripts/replay_downstream.py`, `backend/scripts/run_graphify_assertion_assembler.py`, `backend/services/extraction/graphify_pipeline.py`
- **Tests**: `backend/tests/extraction/test_graphify_assertion_assembler.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_graphify_assertion_assembler.py`
- Size 162 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

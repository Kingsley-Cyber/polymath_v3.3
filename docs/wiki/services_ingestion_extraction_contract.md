# extraction_contract

Source `backend/services/ingestion/extraction_contract.py` (66 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Fail-closed extraction contract for qualified extraction paths.

Synthesis: imported library module; first docstring sentence: “Fail-closed extraction contract for qualified extraction paths.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `resolve_extraction_contract` | 32 | `*, corpus_engine: str \| None, global_engine: str \| None, models_linked: bool \| None, summary_model_count: int, ext...` |
| `ExtractionContract.uses_graphify_cpu` | 28 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `dataclasses`, `services`
- **Imports OUT** (repo-wide): `backend/routers/ingestion.py`, `backend/services/ghost_a.py`, `backend/services/ghost_b.py`, `backend/services/ingestion/batches.py`, `backend/services/ingestion/summary_tree_llm.py`, `backend/services/ingestion/worker.py`
- **Tests**: `backend/tests/test_extraction_contract.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_extraction_contract.py`
- Size 66 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “d7b3245 Complete QueryPlanV2 retrieval hardening”. Full list: `git log --all --oneline -- backend/services/ingestion/extraction_contract.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

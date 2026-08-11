# run_graph_semantic_e2e_phase2_plus

Source `backend/scripts/run_graph_semantic_e2e_phase2_plus.py` (342 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Phases 2–6 on isolated graph_semantic_e2e fixture (no production mutation).

Synthesis: imported library module; first docstring sentence: “Phases 2–6 on isolated graph_semantic_e2e fixture (no production mutation).”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 30 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `json`, `os`, `sys`, `datetime`, `pathlib`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `documents`, `schema_entity_joins`, `summary_information_records`
- **Env vars** (name → default): `GSEM_FIXTURE_CORPUS_ID`→`gsem-e2e-20260804a`, `GSEM_OUT`→`/app/data_eval/knowledge_e2e`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 342 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

# enrichment_executor

Source `backend/services/ingestion/enrichment_executor.py` (135 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> THE enrichment executor — single owner of enrichment execution.

Synthesis: imported library module; first docstring sentence: “THE enrichment executor — single owner of enrichment execution.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `executor_enabled` | 38 | `()` |
| `run_enrichment_executor` | 59 | `db: Any, ingestion_service: Any` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `logging`, `os`, `datetime`, `typing`
- **Imports OUT** (repo-wide): `backend/main.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `extraction_jobs`, `ingest_batch_items`
- **Env vars** (name → default): `ENRICHMENT_EXECUTOR_BATCH`→`200`, `ENRICHMENT_EXECUTOR_ENABLED`→``, `ENRICHMENT_EXECUTOR_IDLE_SECONDS`→`120`, `INGEST_RUNNERS_ENABLED`→``

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 135 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

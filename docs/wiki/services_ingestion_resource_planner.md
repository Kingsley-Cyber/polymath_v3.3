# resource_planner

Source `backend/services/ingestion/resource_planner.py` (356 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Environment-aware ingestion resource planning.

Synthesis: imported library module; first docstring sentence: “Environment-aware ingestion resource planning.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `current_process_rss_mb` | 136 | `()` |
| `memory_soft_limit_mb` | 140 | `settings: Any \| None=None` |
| `throttle_concurrency_for_rss` | 153 | `requested_concurrency: int, *, settings: Any \| None=None` |
| `detect_system_resources` | 186 | `()` |
| `classify_storage_mode` | 221 | `source_location: str \| None` |
| `classify_embedding_backend` | 231 | `*, config: Any, resources: SystemResources` |
| `classify_extraction_backend` | 246 | `*, extraction_engine: str \| None, extraction_pool: list[Any] \| tuple[Any, ...] \| None` |
| `plan_ingestion_resources` | 259 | `*, config: Any, extraction_engine: str \| None, extraction_pool: list[Any] \| tuple[Any, ...] \| None, source_locatio...` |
| `log_resource_profile` | 344 | `profile: ResourceProfile, *, prefix: str='phase=resource_profile', extra: dict[str, Any] \| None=None` |
| `SystemResources.effective_ram_mb` | 37 | `self` |
| `ResourceProfile.rss_pressure` | 73 | `self` |
| `ResourceProfile.to_log_dict` | 78 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `dataclasses`, `logging`, `os`, `pathlib`, `resource`, `sys`, `typing`
- **Imports OUT** (repo-wide): `backend/services/ghost_b.py`, `backend/services/ingestion/readiness.py`, `backend/services/ingestion/worker.py`
- **Tests**: `backend/tests/test_resource_planner.py`, `backend/tests/test_worker_phases.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_resource_planner.py`, `backend/tests/test_worker_phases.py`
- Size 356 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- Audited in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) sections: “2. Batch/buffer size knobs”, “4. Backpressure / memory-aware guards” — link into the map, do not duplicate it.
- Defect-fix commits: 1 — e.g. “205d77b fix: unblock portable provider-control validation”. Full list: `git log --all --oneline -- backend/services/ingestion/resource_planner.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

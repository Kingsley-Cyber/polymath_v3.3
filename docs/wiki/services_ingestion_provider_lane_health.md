# provider_lane_health

Source `backend/services/ingestion/provider_lane_health.py` (394 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Durable provider-lane health for Ghost B extraction pools.

Synthesis: imported library module; first docstring sentence: “Durable provider-lane health for Ghost B extraction pools.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `summarize_provider_lane_health` | 123 | `events: list[dict[str, Any]], *, min_rate_limit_events: int=5, rate_limit_ratio: float=0.5` |
| `filter_extraction_pool_by_provider_health` | 214 | `pool: list[dict[str, Any]], health: dict[str, Any] \| None` |
| `adapt_provider_pool_concurrency` | 248 | `pool: list[dict[str, Any]], health: dict[str, Any] \| None, *, legacy_prompt_provider_canary: bool=True` |
| `adapt_extraction_pool_concurrency` | 342 | `pool: list[dict[str, Any]], health: dict[str, Any] \| None` |
| `load_recent_provider_lane_health` | 355 | `db: Any, *, corpus_id: str, window_minutes: int=30, limit: int=2000, min_rate_limit_events: int=5, rate_limit_ratio: ...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `datetime`, `typing`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion/readiness.py`, `backend/services/ingestion/summary_provider_pool.py`, `backend/services/ingestion/worker.py`
- **Tests**: `backend/tests/test_provider_lane_health.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `ghost_b_error_events`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_provider_lane_health.py`
- Size 394 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

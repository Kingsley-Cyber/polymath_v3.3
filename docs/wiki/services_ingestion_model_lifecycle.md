# model_lifecycle

Source `backend/services/ingestion/model_lifecycle.py` (513 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Managed remote model lifecycle helpers.

Synthesis: imported library module; first docstring sentence: “Managed remote model lifecycle helpers.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `ensure_model_lifecycle_ready` | 217 | `pool: list[dict[str, Any]] \| None, *, purpose: str` |
| `acquire_model_lifecycle_hold` | 317 | `pool: list[dict[str, Any]] \| None, *, purpose: str, hold_id: str, ensure_ready: bool=True` |
| `release_model_lifecycle_hold` | 364 | `pool: list[dict[str, Any]] \| None, *, purpose: str, hold_id: str` |
| `shutdown_model_lifecycle` | 436 | `pool: list[dict[str, Any]] \| None, *, purpose: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `logging`, `time`, `typing`, `httpx`
- **Imports OUT** (repo-wide): `backend/routers/ingestion.py`, `backend/services/ghost_a.py`, `backend/services/ghost_b.py`, `backend/services/ingestion/batches.py`, `backend/services/ingestion/graph_backfill.py`, `backend/services/ingestion/worker.py`
- **Tests**: `backend/tests/test_ghost_a_summary_fallback.py`, `backend/tests/test_ghost_b_routing_policy.py`, `backend/tests/test_ingest_batches.py`, `backend/tests/test_model_lifecycle.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_ghost_a_summary_fallback.py`, `backend/tests/test_ghost_b_routing_policy.py`, `backend/tests/test_ingest_batches.py`, `backend/tests/test_model_lifecycle.py`
- Size 513 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (513 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “9b1f528 fix: harden and optimize durable ingestion”. Full list: `git log --all --oneline -- backend/services/ingestion/model_lifecycle.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

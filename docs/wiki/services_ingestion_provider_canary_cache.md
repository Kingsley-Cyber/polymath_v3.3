# provider_canary_cache

Source `backend/services/ingestion/provider_canary_cache.py` (101 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Secret-safe provider canary cache shared across corpora and pool roles.

Synthesis: imported library module; first docstring sentence: “Secret-safe provider canary cache shared across corpora and pool roles.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `provider_canary_fingerprint` | 14 | `entry: dict[str, Any]` |
| `load_cached_canary` | 29 | `db: Any, *, entry: dict[str, Any], now: datetime \| None=None` |
| `record_canary` | 55 | `db: Any, *, entry: dict[str, Any], ok: bool, status: int \| None, latency_ms: int \| None, error_class: str \| None=N...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `datetime`, `typing`
- **Imports OUT** (repo-wide): `backend/routers/ingestion.py`
- **Tests**: `backend/tests/test_provider_canary_cache.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_provider_canary_cache.py`
- Size 101 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “9b1f528 fix: harden and optimize durable ingestion”. Full list: `git log --all --oneline -- backend/services/ingestion/provider_canary_cache.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

# engine_routing

Source `backend/services/extraction/engine_routing.py` (220 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Runtime routing for the extraction engine (owner-ordered, 2026-08-09).

Synthesis: imported library module; first docstring sentence: “Runtime routing for the extraction engine (owner-ordered, 2026-08-09).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `active_route` | 61 | `*, force_refresh: bool=False` |
| `routed_sidecar_url` | 80 | `()` |
| `routed_expected_release` | 86 | `()` |
| `routed_sidecar_pool` | 92 | `()` |
| `qualified_releases` | 109 | `route: dict[str, Any] \| None=None` |
| `write_route` | 115 | `db: Any, *, sidecar_url: str, expected_release: str, mode: str, updated_by: str, note: str=''` |
| `clear_route` | 164 | `db: Any` |
| `record_qualified_release` | 169 | `db: Any, *, release: str, evidence: str` |
| `describe_route` | 185 | `()` |
| `invalidate_cache` | 216 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `os`, `threading`, `time`, `typing`
- **Imports OUT** (repo-wide): `backend/polymath_mcp/tools.py`, `backend/routers/health.py`, `backend/services/extraction/relex_sidecar_client.py`, `backend/services/ingestion/fleet_status.py`
- **Tests**: `backend/tests/test_engine_routing_control_plane.py`, `backend/tests/test_polymath_mcp_engine_tool.py`
- **Env vars** (name → default): `MONGODB_DB`→`polymath`, `MONGODB_URI`→``, `RELEX_SIDECAR_URL`→``

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_engine_routing_control_plane.py`, `backend/tests/test_polymath_mcp_engine_tool.py`
- Size 220 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “df987f2 Control-plane hardening: audited deterministic routing + human/agent visibility”. Full list: `git log --all --oneline -- backend/services/extraction/engine_routing.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

## VERIFY

Drift check — grep the repo and confirm these still hold (run `docs/wiki/verify_claims.py`):

- `lane_manager in active_route payload` → backend/services/extraction/engine_routing.py:261-278
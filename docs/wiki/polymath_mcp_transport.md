# transport

Source `backend/polymath_mcp/transport.py` (115 lines) · subsystem [mcp](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> MCP transport selector + Starlette auth middleware.

Synthesis: imported library module; first docstring sentence: “MCP transport selector + Starlette auth middleware.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `build_streamable_app` | 77 | `mcp_server` |
| `run_stdio` | 112 | `mcp_server` |
| `JWTAuthMiddleware.dispatch` | 43 | `self, request: Request, call_next` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `contextlib`, `config`, `starlette`, `starlette`, `starlette`, `starlette`, `starlette`, `auth`
- **Imports OUT**: none found — dead-code candidate.

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 115 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

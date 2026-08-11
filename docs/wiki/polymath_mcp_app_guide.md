# app_guide

Source `backend/polymath_mcp/app_guide.py` (610 lines) · subsystem [mcp](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Agent-facing Polymath app guide shared by MCP surfaces.

Synthesis: imported library module; first docstring sentence: “Agent-facing Polymath app guide shared by MCP surfaces.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `get_app_guide` | 560 | `detail: Literal['summary', 'full']='summary'` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `copy`, `typing`
- **Imports OUT** (repo-wide): `backend/routers/mcp_info.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 610 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (610 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 2 — e.g. “b841b55 MCP execution plane: extraction-engine tool + conservation in verify + guided workflow”. Full list: `git log --all --oneline -- backend/polymath_mcp/app_guide.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

# graph_projection_control_plane_close

Source `backend/scripts/graph_projection_control_plane_close.py` (201 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> q9 Graph Projection Control-Plane closure — orphan classify + project + certify.

Synthesis: imported library module; first docstring sentence: “q9 Graph Projection Control-Plane closure — orphan classify + project + certify.”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 23 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `json`, `os`, `sys`, `datetime`, `pathlib`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `extraction_jobs`
- **Env vars** (name → default): `GPROJ_CORPUS_ID`→`6a766597-29f3-4a3e-8918-5de10f0053b3`, `GPROJ_OUT`→`/app/data_eval/q9_final`

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/GRAPH_PROJECTION_CONTROL_PLANE_CLOSEOUT_20260804.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 201 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

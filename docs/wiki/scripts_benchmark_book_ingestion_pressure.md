# benchmark_book_ingestion_pressure

Source `backend/scripts/benchmark_book_ingestion_pressure.py` (226 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Book-ingestion pressure baseline (Part 7/8 audit deliverable).

Synthesis: imported library module; first docstring sentence: “Book-ingestion pressure baseline (Part 7/8 audit deliverable).”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `env` | 63 | `()` |
| `hist` | 72 | `db, coll: str` |
| `queue_depths` | 79 | `db` |
| `control_plane` | 83 | `db` |
| `mongo_load` | 116 | `db` |
| `qdrant_load` | 127 | `()` |
| `worker_env` | 140 | `()` |
| `container_pressure` | 153 | `()` |
| `main` | 171 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `json`, `subprocess`, `sys`, `datetime`, `pathlib`, `pymongo`, `qdrant_client`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/BOOK_INGESTION_CONTROL_PLANE_AUDIT_20260803.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 226 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

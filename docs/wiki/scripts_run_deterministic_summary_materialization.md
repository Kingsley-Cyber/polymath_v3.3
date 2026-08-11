# run_deterministic_summary_materialization

Source `backend/scripts/run_deterministic_summary_materialization.py` (232 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> P3 — materialize the deterministic summary chain on a fixture corpus.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/run_deterministic_summary_materialization.py`), not a runtime service; first docstring sentence: “P3 — materialize the deterministic summary chain on a fixture corpus.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus`, `--cycles`, `--limit`, `--out`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 90 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `json`, `os`, `sys`, `datetime`, `pathlib`, `dotenv`, `motor`, `qdrant_client`, `config`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `documents`, `parent_chunks`, `summary_jobs`, `summary_tree`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 232 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

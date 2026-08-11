# polymath_failed_chunk_backfill

Source `backend/scripts/polymath_failed_chunk_backfill.py` (482 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Plan or run bounded Ghost B failed-chunk retries for a corpus.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/polymath_failed_chunk_backfill.py`), not a runtime service; first docstring sentence: “Plan or run bounded Ghost B failed-chunk retries for a corpus.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus-id`, `--limit`, `--doc-id`, `--max-failed-chunks`, `--smallest-first`, `--run-id`, `--apply`, `--force-active-ingest`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 444 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `json`, `logging`, `sys`, `uuid`, `datetime`, `pathlib`, `typing`, `motor`, `config`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `documents`, `ghost_b_extractions`, `ingest_batches`, `ingest_repair_runs`

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/TEMPORAL_CONTRACT_V1.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 482 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (482 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

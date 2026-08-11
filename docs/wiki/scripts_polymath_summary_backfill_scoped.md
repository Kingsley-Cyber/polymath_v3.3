# polymath_summary_backfill_scoped

Source `backend/scripts/polymath_summary_backfill_scoped.py` (376 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Plan or run bounded parent-summary backfill for an existing corpus.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/polymath_summary_backfill_scoped.py`), not a runtime service; first docstring sentence: “Plan or run bounded parent-summary backfill for an existing corpus.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus-id`, `--limit`, `--batch`, `--run-id`, `--apply`, `--force-active-ingest`, `--no-index`, `--index-only`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 342 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `json`, `logging`, `sys`, `uuid`, `datetime`, `pathlib`, `typing`, `motor`, `qdrant_client`, `config`, `services`, `services`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Tests**: `backend/tests/test_summary_backfill_scoped.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `ingest_batches`, `ingest_repair_runs`, `parent_chunks`

## 4. Linked scripts & configs

- Referenced by docs: `docs/REBATCH_RUNBOOK_2026-07-14.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_summary_backfill_scoped.py`
- Size 376 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

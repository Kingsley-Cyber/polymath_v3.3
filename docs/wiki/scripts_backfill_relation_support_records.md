# backfill_relation_support_records

Source `backend/scripts/backfill_relation_support_records.py` (271 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Backfill Mongo relation_support_records from staged Ghost B extractions.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/backfill_relation_support_records.py`), not a runtime service; first docstring sentence: “Backfill Mongo relation_support_records from staged Ghost B extractions.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--mongo-uri`, `--database`, `--corpus-id`, `--batch-size`, `--max-batches`, `--dry-run`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `backfill` | 178 | `*, mongo_uri: str, database: str \| None, corpus_id: str \| None, batch_size: int, max_batches: int, dry_run: bool` |
| `parse_args` | 238 | `()` |
| `main` | 249 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `os`, `sys`, `datetime`, `pathlib`, `typing`, `motor`, `services`, `services`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Tests**: `backend/tests/test_graph_backfill_metadata_script.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `documents`, `ghost_b_extractions`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_graph_backfill_metadata_script.py`
- Size 271 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

# backfill_relates_to_support_metadata

Source `backend/scripts/backfill_relates_to_support_metadata.py` (202 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Backfill materialized RELATES_TO support metadata in Neo4j.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/backfill_relates_to_support_metadata.py`), not a runtime service; first docstring sentence: “Backfill materialized RELATES_TO support metadata in Neo4j.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--uri`, `--user`, `--password`, `--batch-size`, `--max-batches`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `backfill` | 129 | `*, uri: str, user: str, password: str, batch_size: int, max_batches: int` |
| `parse_args` | 171 | `()` |
| `main` | 181 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `os`, `typing`, `neo4j`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Tests**: `backend/tests/test_graph_backfill_metadata_script.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_graph_backfill_metadata_script.py`
- Size 202 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

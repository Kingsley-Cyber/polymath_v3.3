# backfill_summary_tree_metadata

Source `backend/scripts/backfill_summary_tree_metadata.py` (575 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Backfill deterministic routing metadata onto existing summary-tree rows.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/backfill_summary_tree_metadata.py`), not a runtime service; first docstring sentence: “Backfill deterministic routing metadata onto existing summary-tree rows.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus-id`, `--apply`, `--verify`, `--force`, `--expected-count`, `--backup-dir`, `--restore-backup`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `build_plans` | 122 | `tree_rows: list[dict[str, Any]], parent_rows: list[dict[str, Any]], *, run_id: str, captured_at: str, force: bool=False` |
| `run_corpus` | 318 | `db: Any, *, corpus_id: str, apply: bool, backup_dir: Path \| None, expected_count: int \| None, force: bool` |
| `restore_backup` | 478 | `db: Any, path: Path, *, apply: bool` |
| `main` | 558 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `hashlib`, `json`, `os`, `sys`, `uuid`, `datetime`, `pathlib`, `typing`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Tests**: `backend/tests/test_summary_tree_metadata_backfill.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `parent_chunks`, `summary_tree`
- **Env vars** (no default captured): `MONGODB_URI`, `MONGODB_URL`, `MONGO_URL`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_summary_tree_metadata_backfill.py`
- Size 575 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (575 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

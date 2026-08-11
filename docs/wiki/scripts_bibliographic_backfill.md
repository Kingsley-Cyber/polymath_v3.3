# bibliographic_backfill

Source `backend/scripts/bibliographic_backfill.py` (681 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> T-HOOK-3 / P2.1 deterministic bibliographic backfill (documents only).

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/bibliographic_backfill.py`), not a runtime service; first docstring sentence: “T-HOOK-3 / P2.1 deterministic bibliographic backfill (documents only).”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus-id`, `--apply`, `--verify`, `--force`, `--restore-backup`, `--head-chars`, `--limit`, `--backup-dir`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `plan_for_document` | 179 | `doc: dict, head_text: str, *, captured_at: str \| None=None, run_id: str \| None=None` |
| `coverage` | 333 | `db, corpus_id: str` |
| `corpora_map` | 343 | `db` |
| `run_corpus` | 413 | `db, corpus_id: str, corpus_name: str, *, apply: bool, force: bool, head_chars: int, backup_dir: Path \| None, limit: ...` |
| `restore_backup` | 567 | `db, path: Path, *, apply: bool` |
| `main` | 657 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `hashlib`, `json`, `os`, `re`, `sys`, `uuid`, `collections`, `datetime`, `pathlib`, `typing`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Tests**: `backend/tests/test_bibliographic_backfill_e2e.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `corpora`, `documents`, `parent_chunks`
- **Env vars** (no default captured): `BIBLIO_BACKUP_DIR`, `MONGODB_URI`, `MONGODB_URL`, `MONGO_URL`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_bibliographic_backfill_e2e.py`
- Size 681 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (681 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

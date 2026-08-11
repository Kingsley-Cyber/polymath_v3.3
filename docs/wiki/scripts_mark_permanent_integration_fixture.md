# mark_permanent_integration_fixture

Source `backend/scripts/mark_permanent_integration_fixture.py` (120 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Mark a corpus as the permanent integration fixture (owner directive 2026-08-03).

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/mark_permanent_integration_fixture.py`), not a runtime service; first docstring sentence: “Mark a corpus as the permanent integration fixture (owner directive 2026-08-03).”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--apply`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 80 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `datetime`, `os`, `sys`, `pathlib`, `urllib`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/BOOK_INGESTION_P5_CANARY_REPORT_20260803.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 120 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

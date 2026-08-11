# backfill_child_domain

Source `backend/scripts/backfill_child_domain.py` (176 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Backfill child-chunk `domain` from Ghost-A parent domains (M1, 2026-07-02).

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/backfill_child_domain.py`), not a runtime service; first docstring sentence: “Backfill child-chunk `domain` from Ghost-A parent domains (M1, 2026-07-02).”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus`, `--all`, `--dry-run`, `--verify`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 148 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `logging`, `config`, `motor`, `qdrant_client`, `qdrant_client`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `corpora`, `parent_chunks`

## 4. Linked scripts & configs

- Referenced by docs: `docs/DISPOSITION_MATRIX_2026-07-13.md`, `docs/archive/CONTINUITY/METADATA_LAYER_AUDIT.md`, `docs/archive/CONTINUITY/QDRANT_PAYLOAD_INDEX_AUDIT_20260803.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 176 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

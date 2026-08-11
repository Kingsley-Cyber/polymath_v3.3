# reconcile_verified_documents

Source `backend/scripts/reconcile_verified_documents.py` (212 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Reverify completed document artifacts without re-embedding or model calls.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/reconcile_verified_documents.py`), not a runtime service; first docstring sentence: “Reverify completed document artifacts without re-embedding or model calls.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus-id`, `--doc-id`, `--apply`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run` | 51 | `args: argparse.Namespace` |
| `main` | 202 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `datetime`, `typing`, `motor`, `neo4j`, `qdrant_client`, `config`, `services`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `document_pipeline_jobs`, `documents`, `parent_chunks`, `summary_tree`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 212 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- `main` body is verbatim-identical to `backend/scripts/repair_nonsemantic_source_shells.py` (AST dump hash match) — dedup candidate.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

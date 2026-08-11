# repair_nonsemantic_source_shells

Source `backend/scripts/repair_nonsemantic_source_shells.py` (201 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Exclude zero-chunk cover/navigation shells from corpus readiness.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/repair_nonsemantic_source_shells.py`), not a runtime service; first docstring sentence: “Exclude zero-chunk cover/navigation shells from corpus readiness.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus-id`, `--doc-id`, `--apply`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `inspect_document` | 42 | `db: Any, *, corpus_id: str, doc_id: str` |
| `apply_exclusion` | 93 | `db: Any, *, corpus_id: str, doc_id: str` |
| `run` | 168 | `args: argparse.Namespace` |
| `main` | 191 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `mimetypes`, `datetime`, `pathlib`, `typing`, `motor`, `config`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `document_pipeline_jobs`, `documents`, `graph_promotion_jobs`, `parent_chunks`, `source_parse_jobs`, `summary_jobs`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 201 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- `main` body is verbatim-identical to `backend/scripts/reconcile_verified_documents.py` (AST dump hash match) — dedup candidate.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

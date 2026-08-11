# backfill_corpus_lexicon

Source `backend/scripts/backfill_corpus_lexicon.py` (590 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Backfill the corpus vocabulary bridge from durable extraction artifacts.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/backfill_corpus_lexicon.py`), not a runtime service; first docstring sentence: “Backfill the corpus vocabulary bridge from durable extraction artifacts.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus-id`, `--apply`, `--doc-limit`, `--resume-after-doc-id`, `--skip-vector-index`, `--source-only`, `--materialize-only`, `--gloss-only`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run` | 147 | `*, corpus_ids: list[str], apply: bool, doc_limit: int, resume_after_doc_id: str \| None, skip_vector_index: bool, sou...` |
| `parse_args` | 471 | `()` |
| `main` | 559 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `json`, `logging`, `sys`, `uuid`, `datetime`, `pathlib`, `typing`, `motor`, `qdrant_client`, `config`, `services`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `documents`, `ghost_b_extractions`, `ingest_batches`, `ingest_repair_runs`

## 4. Linked scripts & configs

- Referenced by docs: `docs/EXECUTION_PLAN_2026-07-13.md`, `docs/POLYMATH_INGESTION_RETRIEVAL_JOURNEY.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 590 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (590 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

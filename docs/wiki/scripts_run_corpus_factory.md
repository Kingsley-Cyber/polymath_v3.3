# run_corpus_factory

Source `backend/scripts/run_corpus_factory.py` (98 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Corpus factory gate — serial corpus digest == overlapped corpus digest.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/run_corpus_factory.py`), not a runtime service; first docstring sentence: “Corpus factory gate — serial corpus digest == overlapped corpus digest.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--inputs`, `--max-active`, `--run-id`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 87 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `hashlib`, `os`, `re`, `sys`, `time`, `pathlib`, `types`, `motor`, `dotenv`, `services`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- Reads `.env` via `dotenv_values`.

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 98 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

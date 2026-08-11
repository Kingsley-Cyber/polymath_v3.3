# polymath_graph_replay_backlog

Source `backend/scripts/polymath_graph_replay_backlog.py` (554 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Plan or run bounded Ghost B full-replay graph repair for a corpus.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/polymath_graph_replay_backlog.py`), not a runtime service; first docstring sentence: “Plan or run bounded Ghost B full-replay graph repair for a corpus.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus-id`, `--limit`, `--max-chunks`, `--largest-first`, `--reason`, `--flush-only`, `--run-id`, `--apply`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 505 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `json`, `logging`, `os`, `sys`, `uuid`, `datetime`, `pathlib`, `typing`, `motor`, `config`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Tests**: `backend/tests/test_graph_replay_backlog.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `documents`, `ingest_batches`, `ingest_repair_runs`, `parent_chunks`

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/TEMPORAL_CONTRACT_V1.md`, `docs/archive/COORDINATION.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_graph_replay_backlog.py`
- Size 554 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (554 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

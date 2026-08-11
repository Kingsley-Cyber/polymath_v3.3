# replay_downstream

Source `backend/scripts/replay_downstream.py` (441 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Downstream replay harness — freeze upstream observations, replay the compiler.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/replay_downstream.py`), not a runtime service; first docstring sentence: “Downstream replay harness — freeze upstream observations, replay the compiler.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--source-db`, `--frozen-dir`, `--export-frozen`, `--answer-key`, `--json-out`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `replay` | 131 | `frozen: dict` |
| `waterfall` | 196 | `state: dict` |
| `first_loss` | 272 | `state: dict, gold_path: str` |
| `main` | 376 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `json`, `os`, `re`, `sys`, `collections`, `pathlib`, `models`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `graphify_stage_artifacts`
- Reads `.env` via `dotenv_values`.

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 441 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (441 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “cf42506 feat(graphify): downstream replay harness — frozen observations, conservation waterfall, first-loss traces”. Full list: `git log --all --oneline -- backend/scripts/replay_downstream.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

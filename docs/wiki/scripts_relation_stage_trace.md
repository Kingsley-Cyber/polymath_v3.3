# relation_stage_trace

Source `backend/scripts/relation_stage_trace.py` (412 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Where does a CORRECT relation die? Per-stage attribution against blind gold.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/relation_stage_trace.py`), not a runtime service; first docstring sentence: “Where does a CORRECT relation die? Per-stage attribution against blind gold.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--gold`, `--chunks`, `--textacy`, `--strict`, `--show`, `--show-matches`, `--diagnose`, `--json-out`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `make_matcher` | 66 | `strict: bool` |
| `main` | 83 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `difflib`, `json`, `os`, `sys`, `collections`, `pathlib`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Referenced by docs: `docs/baselines/GLINER_RELEX_EVALUATION_2026-07-31.md`, `docs/baselines/RELATION_RECALL_ATTRIBUTION_2026-07-31.md`, `docs/baselines/RELEX_PRECISION_GATE_RESULT_2026-07-31.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 412 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (412 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

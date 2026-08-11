# qualify_relex_batching

Source `backend/scripts/qualify_relex_batching.py` (309 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Qualify batched Relex MPS inference against the frozen gold set.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/qualify_relex_batching.py`), not a runtime service; first docstring sentence: “Qualify batched Relex MPS inference against the frozen gold set.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--sizes`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `diff_rows` | 89 | `b1: dict, bn: dict, sid: str` |
| `score_with_goldscore` | 126 | `pred_path: Path, tag: str` |
| `main` | 175 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `json`, `os`, `subprocess`, `sys`, `collections`, `pathlib`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/MPS_BATCH_QUALIFICATION_20260805.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 309 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

# p0_1_summary_integrity

Source `backend/scripts/p0_1_summary_integrity.py` (568 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> P0.1 summary-integrity repair driver (checklist: P0 - Summary Integrity).

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/p0_1_summary_integrity.py`), not a runtime service; first docstring sentence: “P0.1 summary-integrity repair driver (checklist: P0 - Summary Integrity).”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus`, `--apply`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `retire_orphan_jobs` | 133 | `apply: bool` |
| `stamp_legacy` | 204 | `corpus: str \| None, apply: bool` |
| `quarantine_regen` | 316 | `corpus: str \| None, apply: bool` |
| `residual` | 393 | `corpus: str \| None, apply: bool` |
| `verify` | 470 | `corpus: str \| None` |
| `main` | 540 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `json`, `re`, `sys`, `time`, `urllib`, `pathlib`, `typing`, `urllib`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Referenced by docs: `docs/EXECUTION_PLAN_2026-07-13.md`, `docs/PLAN_CRITIQUE_2026-07-13.md`, `docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md`, `docs/REBATCH_RUNBOOK_2026-07-14.md`, `docs/execution_plan_audit/raw/frozen_checklist_f049041.md`, `docs/execution_plan_audit/raw/parts/part_10_HDR.md` …
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 568 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (568 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

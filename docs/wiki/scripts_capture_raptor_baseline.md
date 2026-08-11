# capture_raptor_baseline

Source `backend/scripts/capture_raptor_baseline.py` (302 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Reproducible RAPTOR baseline census (read-only).

Synthesis: imported library module; first docstring sentence: “Reproducible RAPTOR baseline census (read-only).”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 118 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `base64`, `json`, `subprocess`, `sys`, `time`, `urllib`, `pathlib`, `typing`, `urllib`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Referenced by docs: `docs/EXECUTION_PLAN_2026-07-13.md`, `docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md`, `docs/execution_plan_audit/raw/frozen_checklist_f049041.md`, `docs/execution_plan_audit/raw/parts/part_10_HDR.md`, `docs/execution_plan_audit/raw/parts/part_10_P0S.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 302 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

# orphan_ownership_manifest

Source `backend/scripts/orphan_ownership_manifest.py` (213 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Dry-run ownership manifest for historical artifacts (checklist P0.6).

Synthesis: imported library module; first docstring sentence: “Dry-run ownership manifest for historical artifacts (checklist P0.6).”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 108 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `base64`, `json`, `sys`, `time`, `urllib`, `pathlib`, `typing`, `urllib`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Referenced by docs: `docs/EXECUTION_PLAN_2026-07-13.md`, `docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md`, `docs/execution_plan_audit/raw/frozen_checklist_f049041.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 213 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “0dc5a7e P0.6: cleanup lease hardening + dry-run orphan ownership manifest”. Full list: `git log --all --oneline -- backend/scripts/orphan_ownership_manifest.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

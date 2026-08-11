# relex_generate

Source `backend/scripts/relex_generate.py` (58 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Phase A: run GLiNER-Relex on host (has torch+gliner), dump raw relations. Phase B applies the repo gate inside the container (has backend+ontology).

Synthesis: imported library module; first docstring sentence: “Phase A: run GLiNER-Relex on host (has torch+gliner), dump raw relations. Phase B applies the repo gate inside the container (has backend+ontology).”.

Imported module only (no `__main__`).

## 2. Entry points

None public (all `_`-prefixed); module-level side effects NOT EXAMINED.

## 3. Dependencies

- **Imports IN** (first-party stems): `json`, `sys`, `time`, `gliner`
- **Imports OUT**: none found — dead-code candidate.

## 4. Linked scripts & configs

- Referenced by docs: `docs/baselines/RELEX_PRECISION_GATE_RESULT_2026-07-31.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 58 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

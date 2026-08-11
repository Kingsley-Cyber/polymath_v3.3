# s4_quarantine_stale

Source `backend/scripts/s4_quarantine_stale.py` (64 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> S4 driver: quarantine capture-stale summaries (valid summary, no latent_concepts) so the scoped backfill regenerates them through the new capture contract. Backup-first (full rows, JSONL). Usage: python s4_quarantine_stale.py <corpus_regex> <limit|all> [--apply]

Synthesis: imported library module; first docstring sentence: “S4 driver: quarantine capture-stale summaries (valid summary, no latent_concepts) so the scoped backfill regenerates them through the new capture contract. Back”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 15 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `asyncio`, `json`, `sys`, `time`, `pathlib`, `motor`, `config`
- **Imports OUT**: none found — dead-code candidate.

## 4. Linked scripts & configs

- Referenced by docs: `docs/REBATCH_RUNBOOK_2026-07-14.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 64 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

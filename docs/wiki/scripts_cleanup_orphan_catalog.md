# cleanup_orphan_catalog

Source `backend/scripts/cleanup_orphan_catalog.py` (409 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> One-shot orphan catalog sweeper for the intended 3+1 live corpus set.

Synthesis: imported library module; first docstring sentence: “One-shot orphan catalog sweeper for the intended 3+1 live corpus set.”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `purge_neo4j_corpus` | 112 | `env: dict[str, str], corpus_id: str` |
| `scrub_tier0` | 225 | `active_ids: set[str]` |
| `main` | 261 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `base64`, `json`, `sys`, `time`, `urllib`, `urllib`, `pathlib`, `typing`, `urllib`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Referenced by docs: `docs/EXECUTION_PLAN_2026-07-13.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 409 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (409 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

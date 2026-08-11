# run_oracle_adapter_qual

Source `backend/scripts/run_oracle_adapter_qual.py` (172 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Oracle-adapter qualification run (owner-authorized 2026-08-08).

Synthesis: imported library module; first docstring sentence: “Oracle-adapter qualification run (owner-authorized 2026-08-08).”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run` | 61 | `()` |
| `offset_mapper` | 73 | `ours, theirs` |
| `main` | 83 | `()` |
| `_Collection.find_one` | 38 | `self, query, projection=None` |
| `_Collection.update_one` | 40 | `self, query, update, upsert=False` |
| `_Collection.insert_one` | 50 | `self, row` |
| `_Collection.count_documents` | 52 | `self, query` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `difflib`, `json`, `os`, `re`, `sys`, `copy`, `pathlib`, `types`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `graphify_stage_artifacts`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 172 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

# export_heldout_submission

Source `backend/scripts/export_heldout_submission.py` (302 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Export held-out submission artifacts from the factory Mongo per contract.

Synthesis: imported library module; first docstring sentence: “Export held-out submission artifacts from the factory Mongo per contract.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `contract_type` | 36 | `internal_type: str, facet: str` |
| `payloads` | 52 | `stage` |
| `did_for` | 66 | `doc_id, filename_by_doc` |
| `match_did` | 77 | `fname` |

## 3. Dependencies

- **Imports IN** (first-party stems): `json`, `re`, `sys`, `time`, `pathlib`, `dotenv`, `pymongo`
- **Imports OUT**: none found — dead-code candidate.
- Reads `.env` via `dotenv_values`.

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 302 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- `payloads` body is verbatim-identical to `backend/scripts/export_final_heldout_v2.py` (AST dump hash match) — dedup candidate.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “2feaf26 Held-out closeout round 1: freeze exception verified, packet becomes development data”. Full list: `git log --all --oneline -- backend/scripts/export_heldout_submission.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

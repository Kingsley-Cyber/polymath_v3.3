# chunk_subprocess

Source `backend/services/ingestion/chunk_subprocess.py` (80 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Lean chunk-stage subprocess entrypoint.

Synthesis: a long-running process entrypoint (`python backend/services/ingestion/chunk_subprocess.py`), typically supervised/compose-launched; first docstring sentence: “Lean chunk-stage subprocess entrypoint.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `chunk_entry` | 37 | `parse_result, doc_id, corpus_id, config` |
| `chunk_entry_pathological` | 48 | `parse_result, doc_id, corpus_id, config` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `copy`, `os`
- **Imports OUT** (repo-wide): `backend/services/ingestion/worker.py`
- **Tests**: `backend/tests/test_worker_phases.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_worker_phases.py`
- Size 80 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- Audited in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) sections: “3. Subprocess memory: OpenIE farm and chunk process pools” — link into the map, do not duplicate it.
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

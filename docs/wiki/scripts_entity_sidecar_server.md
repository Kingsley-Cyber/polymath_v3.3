# entity_sidecar_server

Source `backend/scripts/entity_sidecar_server.py` (142 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Entity-encoder host-MPS sidecar (release entity-sidecar-mps-v1).

Synthesis: imported library module; first docstring sentence: “Entity-encoder host-MPS sidecar (release entity-sidecar-mps-v1).”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 133 | `()` |
| `Handler.do_GET` | 81 | `self` |
| `Handler.do_POST` | 94 | `self` |
| `Handler.log_message` | 129 | `self, fmt, *args` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `json`, `os`, `sys`, `threading`, `time`, `http`, `pathlib`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Env vars** (name → default): `ENTITY_SIDECAR_PORT`→`8738`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 142 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

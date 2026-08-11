# relex_sidecar_server

Source `backend/scripts/relex_sidecar_server.py` (165 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> GLiNER-Relex host-MPS sidecar (release relex-large-mps-sidecar-v1).

Synthesis: imported library module; first docstring sentence: “GLiNER-Relex host-MPS sidecar (release relex-large-mps-sidecar-v1).”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 156 | `()` |
| `Handler.do_GET` | 130 | `self` |
| `Handler.do_POST` | 140 | `self` |
| `Handler.log_message` | 152 | `self, fmt, *args` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `json`, `os`, `sys`, `threading`, `time`, `http`, `pathlib`, `yaml`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Env vars** (no default captured): `RELEX_SIDECAR_ALLOW_CPU`, `RELEX_SIDECAR_CONFIG`, `RELEX_SIDECAR_PORT`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 165 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “3297fc7 CUDA sidecar deployment kit: pinned config, fail-closed guard, release-pin client check”. Full list: `git log --all --oneline -- backend/scripts/relex_sidecar_server.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

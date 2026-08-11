# register_runpod_account

Source `backend/scripts/register_runpod_account.py` (203 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Register or update one Runpod account for multi-account burst routing.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/register_runpod_account.py`), not a runtime service; first docstring sentence: “Register or update one Runpod account for multi-account burst routing.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--name`, `--endpoint-id`, `--embed-endpoint-id`, `--max-workers`, `--request-concurrency`, `--weight`, `--disable`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run` | 78 | `args: argparse.Namespace` |
| `parse_args` | 172 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `json`, `os`, `re`, `sys`, `pathlib`, `typing`, `motor`, `config`, `models`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Env vars** (no default captured): `RUNPOD_ACCOUNT_KEY`

## 4. Linked scripts & configs

- Referenced by docs: `docs/EXECUTION_PLAN_2026-07-13.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 203 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

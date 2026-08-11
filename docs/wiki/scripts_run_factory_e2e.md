# run_factory_e2e

Source `backend/scripts/run_factory_e2e.py` (263 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Factory E2E — production-shaped corpus through the REAL worker path.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/run_factory_e2e.py`), not a runtime service; first docstring sentence: “Factory E2E — production-shaped corpus through the REAL worker path.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--inputs`, `--api-base`, `--corpus-name`, `--timeout`, `--json-out`, `--existing-corpus`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 69 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `json`, `os`, `re`, `sys`, `time`, `collections`, `pathlib`, `httpx`, `dotenv`, `pymongo`, `requests`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- Reads `.env` via `dotenv_values`.

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 263 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “82b37ff O2 crash battery closed + O4 vector-omission conservation + honest e2e gates”. Full list: `git log --all --oneline -- backend/scripts/run_factory_e2e.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

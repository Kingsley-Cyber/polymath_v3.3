# pt9_smoke_test

Source `backend/scripts/pt9_smoke_test.py` (452 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Pt9 end-to-end smoke test.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/pt9_smoke_test.py`), not a runtime service; first docstring sentence: “Pt9 end-to-end smoke test.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--api`, `--corpus-name`, `--timeout-min`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 347 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `json`, `os`, `sys`, `time`, `pathlib`, `httpx`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `documents`
- **Env vars** (name → default): `MONGO_DB`→`polymath`, `NEO4J_PASSWORD`→`neo4j`, `NEO4J_URI`→`bolt://localhost:7687`, `NEO4J_USER`→`neo4j`, `POLYMATH_API`→`http://localhost:8000`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 452 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (452 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

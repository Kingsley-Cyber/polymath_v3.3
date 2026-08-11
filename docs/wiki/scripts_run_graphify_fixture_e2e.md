# run_graphify_fixture_e2e

Source `backend/scripts/run_graphify_fixture_e2e.py` (1137 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Run the committed Graphify fixtures through isolated live stores.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/run_graphify_fixture_e2e.py`), not a runtime service; first docstring sentence: “Run the committed Graphify fixtures through isolated live stores.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--quality`, `--throughput`, `--namespace`, `--proof`, `--input`, `--fixture-name`, `--namespace`, `--report-json`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 1068 | `()` |
| `_Stores.close` | 121 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `hashlib`, `json`, `os`, `re`, `subprocess`, `sys`, `time`, `uuid`, `datetime`, `pathlib`, `typing`, `typing`, `dotenv`, `config`, `models`, `models`, `services`, `motor`, `neo4j`, `qdrant_client`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/verify_graphify_semantic_safety.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `documents`, `ghost_b_extractions`, `graph_projection_jobs`, `graphify_stage_artifacts`
- **Env vars** (no default captured): `GRAPHIFY_E2E_NEO4J_PASSWORD`, `GRAPHIFY_E2E_NEO4J_URI`, `GRAPHIFY_E2E_NEO4J_USER`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 1137 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (1137 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

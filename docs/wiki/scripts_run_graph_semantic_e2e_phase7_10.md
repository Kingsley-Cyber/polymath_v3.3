# run_graph_semantic_e2e_phase7_10

Source `backend/scripts/run_graph_semantic_e2e_phase7_10.py` (466 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Phases 7–10: retrieval, chat/SSE HTML probe, force-recreate replay, closeout.

Synthesis: imported library module; first docstring sentence: “Phases 7–10: retrieval, chat/SSE HTML probe, force-recreate replay, closeout.”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 225 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `hashlib`, `json`, `os`, `sys`, `time`, `datetime`, `pathlib`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `documents`, `schema_entity_joins`, `summary_information_records`, `users`
- **Env vars** (name → default): `GSEM_API`→`http://localhost:8000`, `GSEM_FIXTURE_CORPUS_ID`→`gsem-e2e-20260804a`, `GSEM_OUT`→`/app/data_eval/knowledge_e2e`, `GSEM_PROBE_QUERY`→`What is information retrieval and how does RAG use embeddings?`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 466 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (466 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

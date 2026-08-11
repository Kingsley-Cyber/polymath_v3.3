# tier_chunker

Source `backend/services/ingestion/tier_chunker.py` (1796 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Tier chunker — hierarchical parent/child splitting.

Synthesis: imported library module; first docstring sentence: “Tier chunker — hierarchical parent/child splitting.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `describe_chunking` | 1285 | `parse_result, config=None` |
| `chunk` | 1432 | `parse_result, doc_id: str, corpus_id: str, config=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `logging`, `re`, `dataclasses`, `tiktoken`, `models`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/materialize_gsem_fixture_retrieval.py`, `backend/services/ingestion/chunk_subprocess.py`, `backend/services/ingestion/document_pipeline_executors.py`, `backend/services/ingestion/worker.py`, `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_chunker_routers.py`, `backend/tests/test_explains_links.py`, `backend/tests/test_gap_analysis_repairs.py`, `backend/tests/test_proposition_chunking.py`, `backend/tests/test_tier_chunker.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_chunker_routers.py`, `backend/tests/test_explains_links.py`, `backend/tests/test_gap_analysis_repairs.py`, `backend/tests/test_proposition_chunking.py`, `backend/tests/test_tier_chunker.py`
- Size 1796 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (1796 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “e0d671a Fix tier_chunker CPU-stall on docs with no sentence boundaries”. Full list: `git log --all --oneline -- backend/services/ingestion/tier_chunker.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

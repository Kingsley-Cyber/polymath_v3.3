# graphify_pipeline

Source `backend/services/extraction/graphify_pipeline.py` (969 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Resumable, fail-closed orchestration for the canonical Graphify path.

Synthesis: imported library module; first docstring sentence: “Resumable, fail-closed orchestration for the canonical Graphify path.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run_graphify_pipeline` | 443 | `*, db: Any, corpus_id: str, doc_id: str, text: str, children: Sequence[Any], source_uri: str='', provider: GLiNER2CPU...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `os`, `inspect`, `time`, `dataclasses`, `typing`, `models`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/evals/graphify_synthetic_v1/score_synthetic_family.py`, `backend/scripts/run_graphify_fixture_e2e.py`, `backend/scripts/run_oracle_adapter_qual.py`, `backend/services/extraction/corpus_coordinator.py`, `backend/services/ingestion/graph_backfill.py`, `backend/services/ingestion/route_readiness.py`, `backend/services/ingestion/worker.py`
- **Tests**: `backend/tests/extraction/test_graphify_entrypoint.py`, `backend/tests/extraction/test_graphify_pipeline.py`
- **Env vars** (name → default): `GRAPHIFY_ADAPTER_COMPILER`→``

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_graphify_entrypoint.py`, `backend/tests/extraction/test_graphify_pipeline.py`
- Size 969 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (969 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “82b37ff O2 crash battery closed + O4 vector-omission conservation + honest e2e gates”. Full list: `git log --all --oneline -- backend/services/extraction/graphify_pipeline.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

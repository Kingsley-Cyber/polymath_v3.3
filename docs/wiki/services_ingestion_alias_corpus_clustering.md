# alias_corpus_clustering

Source `backend/services/ingestion/alias_corpus_clustering.py` (1014 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic corpus identity clustering (Phase 6).

Synthesis: imported library module; first docstring sentence: “Deterministic corpus identity clustering (Phase 6).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `cluster_corpus_entities` | 583 | `inventories: Iterable[DocumentEntityInventory] \| None, *, corpus_id: str` |
| `build_inventory` | 938 | `entity: DocumentEntityV1, *, trusted_alias_surfaces: Sequence[str]=(), temporal_former_names: Sequence[str]=(), retri...` |
| `shadow_projection_dicts` | 975 | `batch: CorpusClusteringBatch` |
| `replay_fingerprint` | 997 | `batch: CorpusClusteringBatch` |
| `_UnionFind.add` | 167 | `self, item: str` |
| `_UnionFind.find` | 170 | `self, item: str` |
| `_UnionFind.union` | 177 | `self, a: str, b: str` |
| `_UnionFind.clusters` | 186 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `logging`, `time`, `dataclasses`, `typing`, `models`
- **Imports OUT** (repo-wide): `backend/services/ingestion/alias_shadow_build.py`, `backend/services/ingestion/fixture_knowledge_pipeline.py`
- **Tests**: `backend/tests/test_alias_corpus_clustering_phase6.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_alias_corpus_clustering_phase6.py`
- Size 1014 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (1014 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “c8137a3 perf/fix: promote payload batching, pair-explosion cap, serve-first startup”. Full list: `git log --all --oneline -- backend/services/ingestion/alias_corpus_clustering.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

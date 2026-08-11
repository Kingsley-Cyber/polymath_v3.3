# promote

Source `backend/services/ingestion/promote.py` (925 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic claim-to-graph promotion.

Synthesis: imported library module; first docstring sentence: “Deterministic claim-to-graph promotion.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `promote` | 56 | `extraction: dict[str, Any], *, entity_id_fn: Callable[[str], str] \| None=None` |
| `promoted_index_fields` | 146 | `()` |
| `doc_local_neighbor_chunks` | 156 | `chunk_eids: dict[str, list[str]], cap: int=8` |
| `promote_claims_to_graph` | 713 | `db: Any, neo4j_driver: Any, *, corpus_id: str, doc_id: str \| None=None` |
| `promote_doc` | 794 | `db: Any, *, corpus_id: str, doc_id: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `collections`, `datetime`, `re`, `typing`, `unicodedata`, `models`, `models`, `pydantic`, `services`, `logging`
- **Imports OUT** (repo-wide): `backend/scripts_backfill_promote.py`, `backend/services/ingestion/graph_promotion_jobs.py`, `backend/services/ingestion/worker.py`
- **Tests**: `backend/tests/test_promote.py`, `backend/tests/test_release_stamp_step3.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `ghost_b_extractions`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_promote.py`, `backend/tests/test_release_stamp_step3.py`
- Size 925 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (925 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 2 — e.g. “c8137a3 perf/fix: promote payload batching, pair-explosion cap, serve-first startup”. Full list: `git log --all --oneline -- backend/services/ingestion/promote.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

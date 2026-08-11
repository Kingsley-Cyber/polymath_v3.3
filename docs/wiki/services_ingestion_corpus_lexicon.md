# corpus_lexicon

Source `backend/services/ingestion/corpus_lexicon.py` (3435 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Durable corpus vocabulary materialization from existing extraction artifacts.

Synthesis: imported library module; first docstring sentence: “Durable corpus vocabulary materialization from existing extraction artifacts.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `utcnow` | 161 | `()` |
| `normalize_identity` | 200 | `value: Any` |
| `mine_acronym_pairs` | 231 | `value: Any` |
| `mine_entity_text_evidence` | 278 | `value: Any, canonical_name: str, *, acronym_pairs: list[dict[str, str]] \| None=None` |
| `mine_structural_contexts` | 355 | `heading_path: Any, canonical_name: str` |
| `clean_alias` | 395 | `value: Any, *, canonical_name: str=''` |
| `clean_definition` | 438 | `value: Any` |
| `build_document_lexicon_sources` | 856 | `db: Any, *, corpus_id: str, doc_id: str, candidate_artifacts: Iterable[CandidateExtractionArtifact] \| None=None` |
| `materialize_entries` | 1632 | `source_rows: list[dict[str, Any]], corpus_id: str` |
| `ensure_lexicon_indexes` | 2065 | `db: Any` |
| `refresh_document_lexicon_sources` | 2155 | `db: Any, *, corpus_id: str, doc_id: str` |
| `materialize_affected_lexicon` | 2289 | `db: Any, *, corpus_id: str, affected_keys: Iterable[str]` |
| `materialize_corpus_lexicon` | 2347 | `db: Any, *, corpus_id: str, materialization_id: str \| None=None, key_batch_size: int=2000, progress_callback: Callab...` |
| `refresh_corpus_lexicon_glosses` | 2574 | `db: Any, *, corpus_id: str, batch_size: int=500` |
| `lexicon_version` | 2665 | `entries: list[dict[str, Any]]` |
| `index_corpus_lexicon_slice` | 2811 | `db: Any, qdrant_client: Any, *, corpus_id: str, resume_after_lexicon_id: str \| None=None, limit: int=10000, batch_si...` |
| `finalize_corpus_lexicon_index` | 2999 | `db: Any, qdrant_client: Any, *, corpus_id: str` |
| `index_corpus_lexicon` | 3103 | `db: Any, qdrant_client: Any, *, corpus_id: str, entries: list[dict[str, Any]] \| None=None, batch_size: int=64` |
| `index_affected_lexicon` | 3224 | `db: Any, qdrant_client: Any, *, corpus_id: str, entries: list[dict[str, Any]], stale_lexicon_ids: list[str] \| None=N...` |
| `refresh_and_index_document_lexicon` | 3330 | `db: Any, qdrant_client: Any, *, corpus_id: str, doc_id: str` |
| `remove_document_lexicon_sources` | 3363 | `db: Any, *, corpus_id: str, doc_id: str` |
| `delete_corpus_lexicon` | 3427 | `db: Any, corpus_id: str` |
| `_UnionFind.add` | 449 | `self, value: str` |
| `_UnionFind.find` | 453 | `self, value: str` |
| `_UnionFind.union` | 460 | `self, left: str, right: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `logging`, `math`, `re`, `unicodedata`, `collections`, `collections`, `datetime`, `itertools`, `typing`, `pymongo`, `models`, `services`
- **Imports OUT** (repo-wide): `backend/evals/semantic_resolution_t9_1_census.py`, `backend/scripts/backfill_corpus_lexicon.py`, `backend/scripts/backfill_summary_tree_index.py`, `backend/services/ingestion/document_semantic_profile.py`, `backend/services/ingestion/document_summaries.py`, `backend/services/ingestion/semantic_resolution.py`, `backend/services/ingestion_service.py`, `backend/services/librarian/card_builder.py`, `backend/services/librarian/shelf_engine.py`, `backend/services/retriever/four_lane_router.py` …
- **Tests**: `backend/tests/test_corpus_lexicon.py`, `backend/tests/test_semantic_resolution.py`, `backend/tests/test_shelf_engine.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `corpora`, `documents`, `ghost_b_extractions`, `parent_chunks`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_corpus_lexicon.py`, `backend/tests/test_semantic_resolution.py`, `backend/tests/test_shelf_engine.py`
- Size 3435 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (3435 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

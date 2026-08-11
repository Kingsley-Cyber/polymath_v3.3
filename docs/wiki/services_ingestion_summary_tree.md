# summary_tree

Source `backend/services/ingestion/summary_tree.py` (1119 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> B3 — the owner summary tree (OWNER_SUMMARY_TREE_DESIGN.md).

Synthesis: imported library module; first docstring sentence: “B3 — the owner summary tree (OWNER_SUMMARY_TREE_DESIGN.md).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `derive_node_concepts` | 40 | `parent_rows: list[dict], cap: int=NODE_CONCEPTS_CAP` |
| `summary_tree_retrieval_text` | 83 | `section_range: str, summary: str` |
| `common_heading_path` | 96 | `rows: Sequence[Any], fallback: str=''` |
| `aggregate_tree_temporal` | 121 | `rows: Sequence[Any]` |
| `index_summary_tree_nodes` | 216 | `*, qdrant_client: Any, db: Any \| None=None, corpus_id: str, nodes: Sequence[TreeNode \| dict[str, Any]], embedding_c...` |
| `group_by_section` | 390 | `parents: Sequence[ParentSummaryIn]` |
| `windows` | 413 | `items: Sequence[ParentSummaryIn], lo: int=ROLLUP_WINDOW_MIN, hi: int=ROLLUP_WINDOW_MAX` |
| `top_terms` | 435 | `counter: dict[str, int], k: int` |
| `build_profile_input` | 439 | `title: str, source_type: str, sections: Sequence[TreeNode], domains: dict[str, int], concepts: dict[str, int]` |
| `build_tree` | 528 | `*, doc_id: str, corpus_id: str, title: str, source_type: str, parents: Sequence[ParentSummaryIn], llm_fn: LlmFn \| No...` |
| `sync_document_profile_from_existing_tree` | 732 | `*, db: Any, doc_id: str, corpus_id: str, force: bool=False` |
| `build_and_store_tree` | 812 | `*, db, doc_id: str, corpus_id: str, llm_fn: LlmFn \| None=None, use_llm: bool=True, heal_missing: bool=True, heal_lim...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `json`, `re`, `dataclasses`, `datetime`, `typing`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/backfill_summary_tree_index.py`, `backend/scripts/backfill_summary_tree_metadata.py`, `backend/scripts/backfill_tree_concepts.py`, `backend/services/ingestion/document_summaries.py`, `backend/services/ingestion/worker.py`
- **Tests**: `backend/tests/test_deterministic_summary_tree.py`, `backend/tests/test_gap_analysis_repairs.py`, `backend/tests/test_summary_tree.py`, `backend/tests/test_summary_tree_metadata_backfill.py`, `backend/tests/test_tree_concepts.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `corpus_lexicon`, `documents`, `ghost_b_extractions`, `parent_chunks`, `summary_tree`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_deterministic_summary_tree.py`, `backend/tests/test_gap_analysis_repairs.py`, `backend/tests/test_summary_tree.py`, `backend/tests/test_summary_tree_metadata_backfill.py`, `backend/tests/test_tree_concepts.py`
- Size 1119 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (1119 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “61ba0b4 ingestion+infra: parent-summary guard rail, MLX memory guardrails, owner topology recorded”. Full list: `git log --all --oneline -- backend/services/ingestion/summary_tree.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

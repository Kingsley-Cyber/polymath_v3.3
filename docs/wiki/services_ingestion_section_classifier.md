# section_classifier

Source `backend/services/ingestion/section_classifier.py` (553 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Section classifier — tag each parent chunk with a `ChunkKind` based on its heading path, so downstream phases can:

Synthesis: imported library module; first docstring sentence: “Section classifier — tag each parent chunk with a `ChunkKind` based on its heading path, so downstream phases can:”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `should_summarize_parent` | 99 | `kind: str \| None` |
| `parent_summary_required_clause` | 104 | `field: str='chunk_kind'` |
| `classify_heading` | 178 | `heading_path: Iterable[str] \| None` |
| `is_noisy` | 202 | `kind: str \| None` |
| `should_skip_ghost_b` | 207 | `kind: str \| None` |
| `reference_signal_count` | 324 | `text: str \| None` |
| `is_reference_block` | 338 | `text: str \| None` |
| `classify_content` | 401 | `text: str \| None` |
| `classify_chunk` | 508 | `heading_path: Iterable[str] \| None, text: str \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `typing`
- **Imports OUT** (repo-wide): `backend/polymath_mcp/tools.py`, `backend/scripts/materialize_gsem_fixture_retrieval.py`, `backend/scripts/polymath_summary_backfill_scoped.py`, `backend/scripts/reclassify_citation_chunks.py`, `backend/scripts/reconcile_verified_documents.py`, `backend/services/chat_orchestrator.py`, `backend/services/control_plane/desired_state.py`, `backend/services/ingestion/batches.py`, `backend/services/ingestion/dedup.py`, `backend/services/ingestion/deterministic_summary.py` …
- **Tests**: `backend/tests/test_code_graph_synthesis.py`, `backend/tests/test_explains_links.py`, `backend/tests/test_graph_citation_noise_fix_e2e.py`, `backend/tests/test_pt8_idempotency.py`, `backend/tests/test_section_classifier.py`, `backend/tests/test_section_classifier_citations.py` …

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_code_graph_synthesis.py`, `backend/tests/test_explains_links.py`, `backend/tests/test_graph_citation_noise_fix_e2e.py`, `backend/tests/test_pt8_idempotency.py`, `backend/tests/test_section_classifier.py`, `backend/tests/test_section_classifier_citations.py`, `backend/tests/test_summary_readiness_baseline.py`, `backend/tests/test_tier_chunker.py`, `backend/tests/test_vector_omission_conservation.py`
- Size 553 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (553 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

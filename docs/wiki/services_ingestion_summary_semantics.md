# summary_semantics

Source `backend/services/ingestion/summary_semantics.py` (928 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> §10.1 — the semantic parent-summary contract (POLYMATH_ARCHITECTURE §10.1).

Synthesis: imported library module; first docstring sentence: “§10.1 — the semantic parent-summary contract (POLYMATH_ARCHITECTURE §10.1).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `parse_latent_concepts` | 114 | `obj: dict` |
| `parse_temporal_semantics` | 148 | `obj: dict, *, source_text: str \| None=None` |
| `looks_like_raw_json_text` | 240 | `value: str \| None` |
| `extract_json_object` | 449 | `text: str, *, required_keys: set[str] \| None=None` |
| `parent_summary_artifact_fields` | 475 | `obj: dict, *, summary: str, domain: str \| None=None, semantic_chunk_type: str \| None=None, key_terms: list[str] \| ...` |
| `source_hash_for_text` | 535 | `source_text: str \| None` |
| `summary_id_for_parent` | 539 | `parent_id: str \| None` |
| `summary_retrieval_text` | 554 | `fields: dict` |
| `canonical_parent_summary_fields` | 652 | `parsed: dict, *, parent_id: str, doc_id: str, corpus_id: str, source_text: str \| None, source_child_ids: list[str] \...` |
| `repair_parent_summary_row` | 712 | `row: dict, *, default_summary_model: str='legacy_unknown', now=None` |
| `parse_semantic_summary` | 794 | `raw: str, *, source_child_ids: list[str] \| None=None, source_text: str \| None=None` |
| `topic_key_for` | 918 | `domain: str \| None, heading_path` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `re`, `datetime`
- **Imports OUT** (repo-wide): `backend/services/ghost_a.py`, `backend/services/ingestion/batches.py`, `backend/services/ingestion/summary_backfill.py`, `backend/services/ingestion/summary_tree.py`, `backend/services/ingestion/verify.py`, `backend/services/ingestion/worker.py`
- **Tests**: `backend/tests/test_parent_summary_contract.py`, `backend/tests/test_summary_backfill_scoped.py`, `backend/tests/test_summary_semantics.py`, `backend/tests/test_summary_semantics_latent.py`, `backend/tests/test_summary_semantics_temporal.py`, `backend/tests/test_worker_phases.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_parent_summary_contract.py`, `backend/tests/test_summary_backfill_scoped.py`, `backend/tests/test_summary_semantics.py`, `backend/tests/test_summary_semantics_latent.py`, `backend/tests/test_summary_semantics_temporal.py`, `backend/tests/test_worker_phases.py`
- Size 928 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (928 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “3462a16 fix: parse summary JSON from mixed model output”. Full list: `git log --all --oneline -- backend/services/ingestion/summary_semantics.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

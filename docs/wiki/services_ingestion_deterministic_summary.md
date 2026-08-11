# deterministic_summary

Source `backend/services/ingestion/deterministic_summary.py` (519 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic ingestion summaries — ``deterministic_summary.v1``.

Synthesis: imported library module; first docstring sentence: “Deterministic ingestion summaries — ``deterministic_summary.v1``.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `canonical_summary_json` | 74 | `value: Any` |
| `deterministic_summary_config_hash` | 84 | `()` |
| `deterministic_summary_id` | 96 | `*, parent_id: str, source_hash: str` |
| `parent_source_hash` | 110 | `text: str` |
| `split_sentences` | 129 | `text: str` |
| `score_sentences` | 133 | `sentences: list[str], *, heading_tokens: frozenset[str], config: dict[str, Any] \| None=None` |
| `representative_sentences` | 165 | `text: str, *, heading_tokens: frozenset[str], limit: int \| None=None` |
| `content_inventory` | 206 | `child_rows: list[dict[str, Any]]` |
| `render_parent_summary` | 214 | `*, topic: str, inventory: dict[str, int], key_points: list[str], entities: list[str], quality_flags: list[str]` |
| `build_deterministic_parent_summary` | 239 | `*, parent_row: dict[str, Any], child_rows: list[dict[str, Any]] \| None=None, extraction_rows: list[dict[str, Any]] \...` |
| `to_parent_summary_write` | 297 | `built: dict[str, Any], *, parent_row: dict[str, Any], updated_at: datetime \| None=None` |
| `build_deterministic_child_record` | 338 | `chunk_row: dict[str, Any], extraction_rows: list[dict[str, Any]] \| None=None` |
| `run_deterministic_parent_summaries` | 402 | `db: Any, *, corpus_id: str, limit: int=25, doc_ids: list[str] \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `re`, `datetime`, `typing`, `models`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion/worker.py`, `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_deterministic_parent_summary.py`, `backend/tests/test_worker_summary_control_plane.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `ghost_b_extractions`, `parent_chunks`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_deterministic_parent_summary.py`, `backend/tests/test_worker_summary_control_plane.py`
- Size 519 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (519 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

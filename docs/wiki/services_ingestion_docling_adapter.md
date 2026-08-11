# docling_adapter

Source `backend/services/ingestion/docling_adapter.py` (2525 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic parse adapter (historically the "docling adapter").

Synthesis: imported library module; first docstring sentence: “Deterministic parse adapter (historically the "docling adapter").”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `finalize_source_meta` | 446 | `result: 'DoclingParseResult', filename: str \| None` |
| `parser_strategy` | 614 | `filename: str, mime: str` |
| `retrievable_content_text` | 769 | `result: 'DoclingParseResult'` |
| `has_retrievable_content` | 790 | `result: 'DoclingParseResult'` |
| `parse_document` | 2400 | `raw_bytes: bytes, filename: str, mime: str, do_ocr: bool=False` |
| `_HTMLMetadataParser.handle_starttag` | 219 | `self, tag: str, attrs: list[tuple[str, str \| None]]` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `os`, `re`, `csv`, `tempfile`, `io`, `dataclasses`, `html`, `pathlib`, `models`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/repair_nonsemantic_source_shells.py`, `backend/services/ingestion/parse_policy.py`, `backend/services/ingestion/worker.py`, `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_bibliographic_capture.py`, `backend/tests/test_chunker_routers.py`, `backend/tests/test_docling_adapter.py`, `backend/tests/test_m2_metadata.py`, `backend/tests/test_q9_parse_policy.py`, `backend/tests/test_tier_chunker.py`
- **Env vars** (name → default): `TABLE_PARSE_MAX_ROWS_PER_SHEET`→`5000`, `TABLE_PARSE_MAX_SHEETS`→`20`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_bibliographic_capture.py`, `backend/tests/test_chunker_routers.py`, `backend/tests/test_docling_adapter.py`, `backend/tests/test_m2_metadata.py`, `backend/tests/test_q9_parse_policy.py`, `backend/tests/test_tier_chunker.py`
- Size 2525 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (2525 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 2 — e.g. “59f8b53 fix: harden ingestion and retrieval contracts”. Full list: `git log --all --oneline -- backend/services/ingestion/docling_adapter.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

# bibliographic

Source `backend/services/ingestion/bibliographic.py` (711 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Bibliographic + date identity for documents (T-HOOK-3 / P2.1).

Synthesis: imported library module; first docstring sentence: “Bibliographic + date identity for documents (T-HOOK-3 / P2.1).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `candidate_from_dict` | 94 | `d: dict` |
| `normalize_date_string` | 151 | `raw: str` |
| `resolve_document_dates` | 202 | `candidates: Iterable[DateCandidate \| dict]` |
| `parse_citation_name` | 324 | `name: str` |
| `filename_year_candidate` | 378 | `filename: str` |
| `extract_text_head_biblio` | 419 | `head_text: str` |
| `normalize_language` | 530 | `raw: Any` |
| `build_provenance` | 544 | `*, method: Optional[str], source: Optional[str], precision: Optional[str]=None, reason: Optional[str]=None, origin: s...` |
| `merge_persisted_bibliographic` | 632 | `incoming: dict, existing: Optional[dict]` |
| `promote_bibliographic` | 687 | `doc: dict` |
| `DateCandidate.as_dict` | 85 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `dataclasses`, `datetime`, `pathlib`, `typing`
- **Imports OUT** (repo-wide): `backend/scripts/bibliographic_backfill.py`, `backend/services/corpus_scope_context.py`, `backend/services/ingestion/docling_adapter.py`, `backend/services/storage/mongo_writer.py`
- **Tests**: `backend/tests/test_bibliographic_capture.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_bibliographic_capture.py`
- Size 711 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (711 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

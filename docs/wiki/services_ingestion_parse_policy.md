# parse_policy

Source `backend/services/ingestion/parse_policy.py` (104 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> q9 deterministic parsing contract (owner directive v1.0, 2026-08-05).

Synthesis: imported library module; first docstring sentence: “q9 deterministic parsing contract (owner directive v1.0, 2026-08-05).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `unsupported_by_policy_result` | 56 | `*, reason: str=REASON_DOCUMENT_REQUIRES_OCR` |
| `pdf_text_is_usable` | 76 | `result: Any` |
| `pdf_requires_ocr_rejection` | 91 | `result: Any, filename: str, mime: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `typing`
- **Imports OUT** (repo-wide): `backend/services/ingestion/worker.py`
- **Tests**: `backend/tests/test_q9_parse_policy.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_q9_parse_policy.py`
- Size 104 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

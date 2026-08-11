# source_identity

Source `backend/services/ingestion/source_identity.py` (387 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic source identity helpers for ingestion guardrails.

Synthesis: imported library module; first docstring sentence: “Deterministic source identity helpers for ingestion guardrails.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `extract_declared_source_url` | 70 | `data: bytes \| str \| None` |
| `extract_youtube_video_id` | 97 | `url: str \| None` |
| `canonicalize_source_url` | 128 | `url: str \| None` |
| `extract_source_title` | 167 | `data: bytes \| str \| None` |
| `build_deterministic_filename` | 240 | `*, filename: str \| None=None, source_url: str \| None=None, content_type: str \| None=None, data: bytes \| str \| No...` |
| `build_source_identity` | 304 | `*, filename: str \| None=None, source_url: str \| None=None, content_type: str \| None=None, data: bytes \| str \| No...` |
| `source_identity_doc_fields` | 362 | `*, source_url: str \| None=None, source_identity: dict[str, Any] \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `mimetypes`, `os`, `re`, `typing`, `urllib`
- **Imports OUT** (repo-wide): `backend/polymath_mcp/tools.py`, `backend/scripts/repair_incidental_source_identities.py`, `backend/services/ingestion/worker.py`, `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_source_identity.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_source_identity.py`
- Size 387 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “aafb5ca ingestion: add source identity guardrails and memory modes”. Full list: `git log --all --oneline -- backend/services/ingestion/source_identity.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

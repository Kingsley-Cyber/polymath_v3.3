# stage_identity

Source `backend/services/ingestion/stage_identity.py` (162 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Stage-level idempotency keys for ingestion jobs.

Synthesis: imported library module; first docstring sentence: “Stage-level idempotency keys for ingestion jobs.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `stable_stage_hash` | 16 | `value: Any` |
| `normalized_text_hash` | 21 | `text: Any` |
| `source_file_hash` | 26 | `doc: dict[str, Any] \| None=None, source_identity: dict[str, Any] \| None=None` |
| `chunk_hash` | 39 | `chunk: dict[str, Any] \| None` |
| `embedding_model_hash` | 47 | `doc: dict[str, Any] \| None` |
| `document_stage_identity` | 61 | `*, doc: dict[str, Any], pipeline_contract_hash: str` |
| `source_parse_stage_identity` | 75 | `*, item: dict[str, Any], batch: dict[str, Any] \| None, source_fingerprint: str, source_parse_contract_hash: str` |
| `extraction_stage_identity` | 110 | `*, chunk: dict[str, Any], doc: dict[str, Any] \| None, extraction_contract_hash: str` |
| `summary_stage_identity` | 129 | `*, source: dict[str, Any], doc: dict[str, Any] \| None, source_hash: str, summary_contract_hash: str` |
| `graph_promotion_stage_identity` | 146 | `*, doc: dict[str, Any], extraction_artifact_ids: list[str] \| tuple[str, ...], graph_contract_hash: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `re`, `typing`
- **Imports OUT** (repo-wide): `backend/services/ingestion/document_pipeline_jobs.py`, `backend/services/ingestion/extraction_jobs.py`, `backend/services/ingestion/failure_reconciliation.py`, `backend/services/ingestion/graph_promotion_jobs.py`, `backend/services/ingestion/source_parse_jobs.py`, `backend/services/ingestion/stage_identity_repair.py`, `backend/services/ingestion/summary_jobs.py`, `backend/services/storage/mongo_writer.py`
- **Tests**: `backend/tests/test_stage_identity.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_stage_identity.py`
- Size 162 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

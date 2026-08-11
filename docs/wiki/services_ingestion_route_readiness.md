# route_readiness

Source `backend/services/ingestion/route_readiness.py` (348 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Route-aware readiness bridge: artifact census -> ReadinessDecision.

Synthesis: imported library module; first docstring sentence: “Route-aware readiness bridge: artifact census -> ReadinessDecision.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `corpus_artifact_census` | 65 | `db: Any, corpus_id: str` |
| `decide_corpus_route_from_census` | 144 | `census: dict[str, Any], *, route: str, certificate_id: str \| None=None` |
| `decide_corpus_route` | 162 | `db: Any, corpus_id: str, route: str, *, certificate_id: str \| None=None` |
| `decision_payload` | 175 | `decision: ReadinessDecision` |
| `route_readiness_payload` | 190 | `db: Any, corpus_ids: list[str], route: str` |
| `build_corpus_certificate` | 240 | `db: Any, corpus_id: str` |
| `materialize_corpus_certificate` | 342 | `db: Any, corpus_id: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `logging`, `datetime`, `typing`, `models`
- **Imports OUT** (repo-wide): `backend/polymath_mcp/tools.py`, `backend/services/chat_orchestrator.py`
- **Tests**: `backend/tests/test_polymath_mcp_query_tools.py`, `backend/tests/test_route_readiness_certificate.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `ghost_b_extractions`, `graphify_stage_artifacts`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_polymath_mcp_query_tools.py`, `backend/tests/test_route_readiness_certificate.py`
- Size 348 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

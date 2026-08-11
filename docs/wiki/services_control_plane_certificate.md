# certificate

Source `backend/services/control_plane/certificate.py` (368 lines) · subsystem [control-plane](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Query-ready certificates and the proof contract.

Synthesis: imported library module; first docstring sentence: “Query-ready certificates and the proof contract.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `contract_fingerprint` | 86 | `contract: dict[str, Any] \| None` |
| `certificate_id_for` | 100 | `*, corpus_id: str, doc_id: str, fingerprint: str` |
| `issue_certificate_if_complete` | 109 | `db: Any, census: dict[str, Any]` |
| `load_certificate` | 168 | `db: Any, *, corpus_id: str, doc_id: str, contract: dict[str, Any] \| None` |
| `build_proof` | 247 | `census: dict[str, Any], *, certificate: dict[str, Any] \| None, blocking: list[dict[str, Any]] \| None=None, recovery...` |
| `doc_readiness_proof` | 280 | `db: Any, qdrant_client: Any, *, corpus_id: str, doc_id: str, issue: bool=True, recovery: dict[str, Any] \| None=None` |
| `corpus_readiness_proof` | 321 | `db: Any, qdrant_client: Any, *, corpus_id: str, doc_ids: list[str] \| None=None, max_docs: int=500` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `logging`, `datetime`, `typing`, `services`
- **Imports OUT** (repo-wide): `backend/polymath_mcp/tools.py`, `backend/routers/control_plane.py`, `backend/services/control_plane/reconciler.py`
- **Tests**: `backend/tests/test_control_plane_v2.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_control_plane_v2.py`
- Size 368 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

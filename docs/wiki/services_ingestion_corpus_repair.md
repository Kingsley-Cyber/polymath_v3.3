# corpus_repair

Source `backend/services/ingestion/corpus_repair.py` (1008 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Bounded corpus repair cycle.

Synthesis: imported library module; first docstring sentence: “Bounded corpus repair cycle.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `build_repair_cycle_summary` | 38 | `*, steps: list[dict[str, Any]], readiness: dict[str, Any] \| None` |
| `run_bounded_corpus_repair_cycle` | 282 | `db: Any, *, corpus_id: str, user_id: str, ingestion_service: Any=None, qdrant_client: Any=None, neo4j_driver: Any=Non...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `datetime`, `typing`, `uuid`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_corpus_repair.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `ingest_repair_runs`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_corpus_repair.py`
- Size 1008 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (1008 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- Audited in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) sections: “4. Backpressure / memory-aware guards”, “4. Chain-argument table for one hypothetical tick” — link into the map, do not duplicate it.
- Defect-fix commits: 2 — e.g. “59f8b53 fix: harden ingestion and retrieval contracts”. Full list: `git log --all --oneline -- backend/services/ingestion/corpus_repair.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

# pressure

Source `backend/services/ingestion/pressure.py` (278 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Ingestion pressure/readiness signals.

Synthesis: imported library module; first docstring sentence: “Ingestion pressure/readiness signals.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `build_ingestion_pressure_snapshot` | 121 | `*, backend_rss_mb: int \| None=None, ram_cap_mb: int \| None=None, rss_soft_limit_mb: int \| None=None, active_repair...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `typing`
- **Imports OUT** (repo-wide): `backend/services/ingestion/readiness.py`
- **Tests**: `backend/tests/test_corpus_readiness.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_corpus_readiness.py`
- Size 278 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- Audited in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) sections: “4. Backpressure / memory-aware guards” — link into the map, do not duplicate it.
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

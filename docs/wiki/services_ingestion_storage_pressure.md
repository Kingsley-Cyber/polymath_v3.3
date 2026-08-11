# storage_pressure

Source `backend/services/ingestion/storage_pressure.py` (235 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Storage service pressure samplers for ingestion readiness.

Synthesis: imported library module; first docstring sentence: “Storage service pressure samplers for ingestion readiness.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `parse_memory_limit_bytes` | 23 | `value: Any` |
| `qdrant_pressure_from_prometheus` | 91 | `metrics_text: str, *, memory_limit_bytes: int \| None=None, memory_warn_ratio: float=0.85, memory_stop_ratio: float=0.9` |
| `sample_qdrant_pressure` | 210 | `qdrant_url: str \| None, *, timeout_s: float=1.0, memory_limit_bytes: int \| None=None, memory_warn_ratio: float=0.85...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `typing`, `httpx`
- **Imports OUT** (repo-wide): `backend/services/ingestion/readiness.py`
- **Tests**: `backend/tests/test_corpus_readiness.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_corpus_readiness.py`
- Size 235 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

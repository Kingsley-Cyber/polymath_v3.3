# extraction_burst

Source `backend/services/ingestion/extraction_burst.py` (403 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Pure P2.7b burst manifest, barrier, metrics, and retry-safety contracts.

Synthesis: imported library module; first docstring sentence: “Pure P2.7b burst manifest, barrier, metrics, and retry-safety contracts.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `build_extraction_burst_manifest` | 188 | `*, corpus_id: str, disposition: CorpusDisposition, source_batch_status: str, active_ingest_items: int, documents: lis...` |
| `durable_retry_safety_decision` | 329 | `*, job: dict[str, Any], extraction_row: dict[str, Any] \| None` |
| `build_extraction_burst_metrics` | 357 | `*, manifest_id: str, eligible_chunks: int, duration_seconds: float, lanes: list[LaneBurstMetrics], estimated_cost_onl...` |
| `ExtractionBurstManifest.validate_manifest_totals` | 71 | `self` |
| `ExtractionBurstManifest.manifest_id` | 90 | `self` |
| `LaneBurstMetrics.validate_lane_counts` | 119 | `self` |
| `ExtractionBurstMetrics.validate_burst_totals` | 167 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `collections`, `typing`, `pydantic`, `models`, `services`
- **Imports OUT**: none found — dead-code candidate.
- **Tests**: `backend/tests/test_extraction_parity_burst.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_extraction_parity_burst.py`
- Size 403 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (403 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

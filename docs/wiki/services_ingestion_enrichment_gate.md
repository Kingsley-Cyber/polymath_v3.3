# enrichment_gate

Source `backend/services/ingestion/enrichment_gate.py` (160 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> E1 — quality-gated RTX enrichment decision (§13-H, owner-ratified).

Synthesis: imported library module; first docstring sentence: “E1 — quality-gated RTX enrichment decision (§13-H, owner-ratified).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `enrichment_verdict` | 38 | `metrics: dict \| None, *, min_coverage: float=0.8, min_facts_per_chunk: float=1.0, max_related_to_ratio: float=0.4` |
| `select_enrichment_tasks` | 84 | `tasks: Sequence[Any], results: Sequence[Any], failures: Sequence[Any], verdict: EnrichmentVerdict, *, max_chunk_ratio...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `math`, `dataclasses`, `typing`
- **Imports OUT**: none found — dead-code candidate.

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 160 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

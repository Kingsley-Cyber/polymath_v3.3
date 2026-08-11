# spacy_relation_adapter

Source `backend/services/extraction/spacy_relation_adapter.py` (437 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> spaCy dependency-path relation adapter for the Ghost B pipeline.

Synthesis: imported library module; first docstring sentence: “spaCy dependency-path relation adapter for the Ghost B pipeline.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `get_spacy_extractor` | 431 | `()` |
| `SpacyRelationExtractor.extract_chunks` | 134 | `self, chunks: list[dict], max_related: int=10, unit_batch: int=64, docs: list \| None=None, suppr...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `os`, `typing`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/gate_v2_sample.py`, `backend/scripts/recall_score.py`, `backend/scripts/relation_stage_trace.py`, `backend/scripts/rpre_measure.py`, `backend/scripts/rpre_sample_relations.py`, `backend/services/extraction/relex_gate.py`, `backend/services/ingestion/organ_repair_jobs.py`
- **Tests**: `backend/tests/test_relation_stage_trace.py`
- **Env vars** (name → default): `GHOST_B_PAIRING_MODEL`→`frame`, `SPACY_MODEL`→`en_core_web_sm`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_relation_stage_trace.py`
- Size 437 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (437 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

# File map — what exists, what's new, what's deprecated

## Already in the repo (touch only as noted)

| file | role | Phase A action |
|---|---|---|
| `backend/services/ghost_b.py` | CLOUD Ghost B (LLM-based). Contains `EntityItem`, `RelationItem`, `FactItem`, `ExtractionResult` dataclasses (line ~1675-1740). | **Read in A.1 for `ExtractionResult` field semantics. Do NOT modify.** Orphaned in the new design but kept as reference. |
| `backend/services/ghost_b_schemas.py` | Pydantic `LLMEntity`, `LLMFact`, `LLMRelation`, `FactType` (the 9-value Literal). | **Read only. Used for validation in A.2.** |
| `backend/services/ingestion/worker.py` | Ingestion orchestrator. `_a_branch()` (Ghost A, cloud, untouched) and `_b_branch()` (Ghost B, this is what A.5 modifies). | **Modify in A.5**: change one import + one call. |
| `backend/services/ingestion/enrich.py` | Pass-1 deterministic: numeric facts + in-text aliases (Schwartz-Hearst) + shared `CUES` regex map. | **Extend in A.4**: add `extract_qualitative_facts()`. |
| `backend/services/ingestion/slm_enrich.py` | Adapter for the (now-deprecated) SLM enrichment via sidecar. | **Leave as-is. Orphaned in the local lane but not deleted.** |
| `local_ghost_b/glirel_infer.py` | Canonical GLiREL inference (sentence-windowed). Used by A.2. | **Reuse as-is.** Import into `ghost_b_local.py`. |
| `local_ghost_b/safety_rules.py` | Re-exports `type_plausible`, `guard_dangerous`. | **Reuse as-is.** Import into `ghost_b_local.py` for the safety gate. |
| `local_ghost_b/pipeline_config.py` | Single source of truth for GLiNER model id, threshold, chunker params, etc. | **Extend in A.3**: add `GHOST_B_FACET_VOCAB`. Also: expand `GHOST_B_ENTITY_TYPES` from 11 to the full 15. |
| `local_ghost_b/heads/glirel_ghost_b_v1/` | Fine-tuned GLiREL weights (`pytorch_model.bin` + `labels.json` + `glirel_config.json`). | **Reuse as-is. Already committed.** |
| `models/glirel_ghost_b_v1/best/` | Same weights at the loader's primary search path. | Used by `glirel_infer.py` at runtime. |
| `scripts/apple_ml_services/slm_enrich_mlx/` | Qwen GGUF sidecar (Pass-2 SLM). | **Deprecated in the new design but not deleted. Don't call it from local Ghost B.** |
| `scripts/apple_ml_services/embedder_mlx/` | Embedder sidecar :8082 (Qwen3-Embedding-0.6B-mxfp8). | **Already local. No changes.** |
| `scripts/apple_ml_services/docling_svc/` | Docling sidecar :8500. | **Already local. No changes.** |
| `backend/config.py` | Pydantic settings. Has `LOCAL_PASS1_ENRICH_ENABLED`, `LOCAL_SLM_ENRICH_ENABLED`, etc. | **Don't add new flags.** The old `LOCAL_SLM_ENRICH_*` flags become no-ops in the new design (their env-gated branch in `worker.py` won't fire because we're not calling the SLM adapter anymore). |

## New files to create in Phase A

| file | created in | purpose |
|---|---|---|
| `backend/services/ghost_b_local.py` | A.2 | The local extractor: `async def extract_entities(...)` matching cloud's signature. |
| `backend/services/ingestion/facet_tagger.py` | A.3 | GLiNER pass-2 for `object_kind` per unique entity. |

## Files to modify in Phase A

| file | task | nature of change |
|---|---|---|
| `backend/services/ingestion/enrich.py` | A.4 | ADD `extract_qualitative_facts()` and merge into the `extract()` entry point. |
| `local_ghost_b/pipeline_config.py` | A.3 | ADD `GHOST_B_FACET_VOCAB`; expand `GHOST_B_ENTITY_TYPES` to the full 15 if not already. |
| `backend/services/ingestion/worker.py` | A.5 | Change `_b_branch()` import + call. Single-line-of-code change conceptually. |

## Files to NOT touch

- `backend/services/ghost_b.py` (cloud extractor)
- `backend/services/ghost_b_schemas.py` (Pydantic schemas)
- `backend/services/ingestion/graph_backfill.py` (Neo4j writer — works on `ExtractionResult` regardless of who produced it)
- `backend/services/embedder.py`
- Any `_a_branch()` / cloud Ghost A code
- The deprecated `slm_enrich.py` adapter and `slm_enrich_mlx` sidecar
- `local_ghost_b/run_on_mac.py` (CLI tool; out of the worker path)

## Git state at start of Phase A

```
HEAD:           53b04ca (Default SLM enrich sidecar to GGUF backend)
origin/main:    53b04ca (synced)
working tree:   clean (per earlier session)
```

Sidecar processes may or may not be running on 8083 / 8082 / 8500 — the new local Ghost B doesn't depend on the SLM sidecar (8083) at all; it does call the embedder (8082) and docling (8500) which were already part of the pipeline.

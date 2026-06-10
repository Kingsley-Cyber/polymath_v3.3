# Locked Decisions — Phase A

Every entry here was explicitly confirmed by the user across the build-up sessions.
**Do not re-open these.** If something looks wrong, flag it and ask before changing.

## Scope

| decision | locked value | source |
|---|---|---|
| Ghost B (entity/relation/fact extraction) | **fully local, deterministic** — no SLM, no Qwen sidecar in the path | user: "id rather do #2 and remove qwen side care" |
| Ghost A (summaries) | **stays cloud** — DO NOT touch worker's `_a_branch()` | user: "summarization will remain as is and be cloud" + "no leave summaries from this im going to keep it cloud api" |
| Cloud-vs-local lane switch / failover / 3-strike routing | **deleted from the design** — local IS the pipeline, not a fallback | user: "get cloud out of your mind think fully local ingestion and extraction" |
| Embedder | **already local** (`embedder_mlx` sidecar :8082, Qwen3-Embedding-0.6B-mxfp8); confirmed by user as the existing setup | user: "i cant remember setup im pretty sure current one you found is the exact setup" |
| Qdrant, Neo4j, docling, tier_chunker | already local; unchanged | repo state |

## Local Ghost B internals

| component | implementation | model / source |
|---|---|---|
| Entity tagging | GLiNER pass-1, in-process MPS | `urchade/gliner_medium-v2.1`, threshold 0.45 |
| Facet labeling (`object_kind`) | GLiNER pass-2, deduped per unique entity, in-process MPS | same GLiNER model, new label vocabulary (see `01_ARCHITECTURE.md`) |
| Relation extraction (30 Ghost B predicates) | GLiREL fine-tuned, sentence-windowed | `models/glirel_ghost_b_v1/best/` (already on disk + GitHub) |
| Numeric facts (quantity/timestamp/threshold/property) | `enrich.py` Pass-1 Python rules + Schwartz-Hearst aliases | existing `backend/services/ingestion/enrich.py` |
| Qualitative facts (status/category/tag/rule_condition/rule_action) | NEW: extend `enrich.py` with rule-based extractors | Phase A.4 |
| In-text aliases | `enrich.py` Schwartz-Hearst (existing) | existing |
| Out-of-text aliases | **skipped** — embedder absorbs alias work at query time; no Wikidata lookup | user: "no leave summaries from this im going to keep it cloud api" (implicit acceptance of trade) |
| Confidence sentinels | Pass-1 deterministic = 1.0; GLiNER = its softmax score; GLiREL = its softmax score; qualitative rules = 0.9 | user: "Local-lane confidence values ... keep at sentinel" |

## What's NOT in Phase A

- No Phase C failover (deleted — no cloud to fall back to)
- No Phase B SLM pool / sidecar fan-out (no SLM at all)
- No UI button changes (Phase D) — deferred
- No Wikidata aliasing (skipped per scope)
- No `LOCAL_GHOST_B_LANE` env switch (no flag; local is the only path)
- No `lane` column in Mongo staging (no lanes)
- No partial-doc / per-doc routing logic (no lanes)
- No Ghost A local summarizer (user kept it cloud)

## Runtime decision (2026-06-09, user-approved direction: full local extraction)

| decision | locked value | source |
|---|---|---|
| Where local Ghost B runs | **native sidecar `ghost_b_extract_svc` on :8084** (pattern-consistent with embedder :8082 / docling :8500); the Dockerized backend calls it over `host.docker.internal`. When the worker itself runs natively (driver scripts, tests), extraction runs **in-process** — `ghost_b_local.extract_entities` auto-detects by probing for torch/gliner/glirel, override via `LOCAL_GHOST_B_EXTRACT_MODE` | feasibility review 2026-06-09: backend container is Linux (no Metal), and `frontend` + `cloudflared` hard-depend on the `backend` compose service (public tunnel routes `api.kingsleylab.xyz -> http://backend:8000` by service name), so running the whole backend natively would dismantle the deployment. Sidecar equivalence test: sidecar output byte-identical to in-process |
| Ghost A (summaries) | **still cloud API** — reconfirmed | user 2026-06-09: "as a reminder summary will remain cloud api llm calls" |
| Sidecar venv | `local_ghost_b/.venv` (pinned torch/gliner/glirel + pydantic/fastapi/uvicorn), NOT the shared apple_ml_services venv | avoids re-fighting the huggingface_hub<1.0 / transformers<5 pin battle |

## Hardware constraint

Mac M1 Max, 32 GB unified RAM, 10-core CPU + 32-core GPU. User confirmed: "24gb of 32gb actually and i agree we will just have to ensure allocation of memory" — but with SLM removed, memory pressure drops significantly. Local Ghost B stack uses ~2.5 GB total (GLiNER 500MB + GLiREL 1.5GB + Python overhead 500MB).

## Behavioral protocol (user-set)

**Predict outcome before executing**, especially for model swaps, multi-step pipelines, destructive ops, or hypothesis-driven requests. See [`06_BEHAVIORAL_RULES.md`](06_BEHAVIORAL_RULES.md) for full rules.

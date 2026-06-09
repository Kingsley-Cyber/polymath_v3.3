# A.6 — Smoke results + the deployment decision

## What was validated (extraction stage — PASS)

Ran `services.ghost_b_local.extract_entities` over the full real file
`/Volumes/Flash Drive/merged/flame_engine_docs_complete.md` (8 child chunks, via
`local_ghost_b/flame_chunks.jsonl`), through the real GLiNER ×2 + GLiREL + enrich
stack with Pydantic validation against the real `ghost_b_schemas`.

| check | result |
|---|---|
| one `ExtractionResult` per task | ✅ 8/8 |
| `ExtractionResult.text` populated (Pt 10b) | ✅ all |
| `schema_version == "polymath.extract.v1"` | ✅ |
| entities / relations / facts | 32 / 17 / 17 |
| object_kind coverage (GLiNER pass-2) | 12/32 ≈ 38% (rest fall through to downstream taxonomy) |
| Pydantic validation drops (entity/relation/evidence/fact) | 0 / 0 / 0 / 0 |
| determinism (same chunk twice → identical repr) | ✅ |
| exceptions | none |

Relation quality (eyeballed): `flame --uses--> flutter`, `bridge packages
--part_of--> flame-engine`, `flame-engine --instance_of--> engine`,
`components --part_of--> flame` — all sensible. Facets: `flame→game_engine`,
`flutter→framework`, `bridge packages→package`.

### Latency
Warm pass (models already loaded): **~577 ms/chunk** on this 8-chunk doc.
Slightly over the 500 ms soft target, because the per-doc GLiNER pass-2 facet
step (one forward pass per UNIQUE entity) amortizes poorly over only 8 chunks.
On a 200+ chunk doc the same unique entities dedup once across far more chunks,
so per-chunk cost drops. Cold model load is ~20 s once per process (GLiNER ~10 s
+ GLiREL 1.87 GB ~10 s), excluded from the warm figure.

**Tuning levers if needed later:** trim the 28-label facet vocab; skip the facet
pass for low-confidence entities; batch the facet predictions.

## What is NOT yet validated (storage stage — BLOCKED on a decision)

The Neo4j-MERGE + Qdrant-embed half of an end-to-end ingest could not run, for a
structural reason worth a decision:

- `polymath_v33-backend-1` (the worker) is a **Linux Docker** service (`uvicorn`
  in `backend/Dockerfile`) and is currently **Exited**.
- A Linux Docker container **cannot use MPS / Metal**. That is exactly why the
  embedder, docling, and slm services run as **native macOS sidecars**, not in
  Docker.
- `ghost_b_local` does **in-process** GLiNER + GLiREL, which wants MPS. The
  locked A.5 design ("just reroute the import") therefore assumes the worker
  runs **natively on macOS**, not in the Docker container.

### The decision (user-owned — cannot be inferred from code)

**How should the local Ghost B run in production?**

1. **Native worker (matches the locked in-process design).** Run the ingestion
   worker natively on macOS (outside Docker) with the `local_ghost_b` venv (now
   incl. pydantic). Zero code change beyond Phase A. GLiNER/GLiREL load
   in-process on MPS. Qdrant/Neo4j/Mongo stay in Docker (they're reachable over
   localhost). **Recommended** — it's what Phase A already targets and needs no
   further work.

2. **Ghost B sidecar (consistent with embedder/docling).** Wrap
   `ghost_b_local` in a small FastAPI sidecar on a port, keep the worker in
   Docker, and have `_b_branch` call it over HTTP. This is a NEW task (a
   "Phase B"): a sidecar app + changing A.5 from an import reroute to an HTTP
   client. More moving parts, but keeps the worker containerized.

Phase A's locked scope is option 1 (in-process). The code is complete and
correct for it. Option 2 would be additive future work, not a change to the
Phase A modules.

### To finish the e2e smoke once the decision is made

- **If native:** start the worker natively (`uvicorn main:app` from `backend/`
  in the native venv with the local stack deps), ensure embedder :8082 is up
  (it is), POST `flame_engine_docs_complete.md` to the ingest endpoint, then
  verify in Neo4j (entities with `object_kind`, relations, facts) and Qdrant
  (chunk + summary vectors).
- **If sidecar:** build the sidecar first (new task), then the above.

## Runtime dependency note

`ghost_b_local` requires, in whatever process runs the worker: `torch` (MPS or
CPU), `gliner`, `glirel`, `pydantic`, and the weights at
`models/glirel_ghost_b_v1/best/` + the GLiNER HF cache. The `local_ghost_b/.venv`
has all of these (pydantic was added during A.6). A native worker should use
that venv or an equivalent.

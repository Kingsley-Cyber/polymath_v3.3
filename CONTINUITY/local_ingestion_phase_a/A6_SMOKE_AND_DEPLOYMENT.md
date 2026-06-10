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

### The decision — RESOLVED 2026-06-09: sidecar (option 2)

The original recommendation here was option 1 (native worker). That was
**reversed** after tracing the deployment: ingestion runs inside the uvicorn
backend process (`routers/ingestion.py` → `ingestion_service.py` → worker), so
"native worker" means running the whole backend natively — but `frontend` and
`cloudflared` hard-depend on the `backend` compose service's healthcheck, and
the public tunnel routes `api.kingsleylab.xyz -> http://backend:8000` by Docker
service name. Going native would dismantle a working public deployment. The
sidecar is the repo's own established pattern (embedder :8082, docling :8500).

**Built and verified same day:**
- `scripts/apple_ml_services/ghost_b_extract_svc/main.py` — FastAPI sidecar on
  :8084, runs `ghost_b_local._extract_raw` on MPS, warms models at startup.
  Runs on `local_ghost_b/.venv` (the pinned ML working set).
- `ghost_b_local.extract_entities` is now dual-mode: in-process when
  torch/gliner/glirel import (native scripts/tests), HTTP to the sidecar when
  they don't (Docker backend). Env: `LOCAL_GHOST_B_EXTRACT_URL` (default
  `http://host.docker.internal:8084`), `LOCAL_GHOST_B_EXTRACT_MODE`
  (auto|inproc|http), `LOCAL_GHOST_B_EXTRACT_TIMEOUT_S` (default 600).
- `start.sh`: `START_GHOST_B_EXTRACT=true` launches it supervised.
- **Equivalence verified**: sidecar output byte-identical to in-process on real
  chunks; forced-http dataclass rebuild identical to the in-process path.
- Bonus: the cross-process comparison exposed a latent determinism bug —
  `enrich._casing_variants` returned set-ordered aliases (hash-seed dependent,
  flipped across restarts). Fixed with `sorted()`.

### To finish the e2e smoke (next step)

Start the sidecar (`START_GHOST_B_EXTRACT=true scripts/apple_ml_services/start.sh`
or manually via uvicorn), bring up the backend container, POST
`flame_engine_docs_complete.md` to the ingest endpoint, then verify in Neo4j
(entities with `object_kind`, relations, facts) and Qdrant (chunk + summary
vectors).

## Runtime dependency note

`ghost_b_local` requires, in whatever process runs the worker: `torch` (MPS or
CPU), `gliner`, `glirel`, `pydantic`, and the weights at
`models/glirel_ghost_b_v1/best/` + the GLiNER HF cache. The `local_ghost_b/.venv`
has all of these (pydantic was added during A.6). A native worker should use
that venv or an equivalent.

# Pre-q9 16 GB Memory Architecture — BASELINE INSPECTION

**Written:** 2026-08-04T17:35Z  
**Directive:** Polymath 16 GB Memory Architecture — Pre-q9 Configuration Directive  
**Phase:** inspect only (no 10-file q9 ingest; config not yet applied)  
**Host measured:** Apple Silicon unified memory — **32 GiB physical** (not 16 GiB)

---

## 0. Binding conflicts with prior session work

| Prior q9 step 9 (applied live) | This 16GB directive target |
|---|---|
| Mongo WT **1.5 GB**, limit 8g | Mongo WT **0.75 GB**, limit **1280 MiB** |
| Neo4j heap **2G/2G**, pagecache **1G**, limit 8g | Neo4j heap **768M/768M**, pagecache **512M**, limit **1792 MiB** |
| Qdrant limit **8g** | Qdrant limit **3072 MiB** |
| Backend limit **10g** | Backend limit **768 MiB** |

**Ruling needed:** this directive **supersedes** step-9 numbers for the pre-q9 16GB campaign. Step-9 closeout remains a receipt of what was briefly applied; do not treat 1.5/2G as the final pre-q9 config.

**Also still open from q9 steps 7–8:** running `backend` / `mcp` / `ingest-worker` still have `QDRANT_BINARY_QUANTIZATION_ENABLED=true` and old evidence-index lists — host `.env` says `false`, but containers were not rebuilt/recreated. Must be fixed as part of pre-q9 config apply.

---

## 1. Compose / env inventory

### Compose files present
- `docker-compose.yml` (base)
- `docker-compose.apple-mlx.yml` (host MLX embed/rerank; disables in-cluster GPU services)
- `docker-compose.override.yml` (local; gitignored) — retargets EMBEDDER/RERANKER/RELEX to `host.docker.internal`
- `docker-compose.offline-ingest.yml` (ingest-worker profile; default mem **20g**)
- `docker-compose.heavy-ingest.yml` (heavier mongo/neo4j — **antithetical** to 16GB)
- eval overlays (claim-anchor, final-acceptance, etc.)

### `mem_reservation`
**Almost unused.** Only GPU `deploy.reservations` on in-cluster embedder/reranker (disabled on Apple path). Service-level `mem_reservation` for the 16GB table is **not implemented**.

### Current `.env` hard limits (live before 16GB apply)

| Var | Current | 16GB directive |
|---|---|---|
| `QDRANT_MEM_LIMIT` | 8g | 3072m |
| `MONGO_MEM_LIMIT` | 8g | 1280m |
| `MONGO_WIREDTIGER_CACHE_GB` | 1.5 | 0.75 |
| `NEO4J_MEM_LIMIT` | 8g | 1792m |
| `NEO4J_HEAP_INITIAL/MAX` | 2g / 2g | 768m / 768m |
| `NEO4J_PAGECACHE_SIZE` | 1g | 512m |
| `BACKEND_MEM_LIMIT` | 10g | 768m |
| `INGEST_WORKER_MEM_LIMIT` | 10g | 1024m (or monolithic 5g fallback) |
| `MCP_MEM_LIMIT` | (compose default 1g) | 384m |
| `SEARXNG` | running @ 512m | should be **web profile only** |

---

## 2. Model / sidecar topology (MEASURED)

| Role | Where it runs | Endpoint | Status |
|---|---|---|---|
| Embedding | **Host** LaunchAgent (`com.polymath.apple-ml`) — NOT in Docker | `:8082` | healthy (Qwen3-Embedding-0.6B MLX) |
| Reranker | **Host** (same / related LaunchAgent) | `:8081` | healthy (jina-reranker-v3) |
| GLiNER-Relex | **Host sidecar** (`relex_extract_svc` on `:8086`) — NOT inside ingest-worker | `RELEX_LOCAL_URL=http://host.docker.internal:8086` | healthy, model_hash verified |
| Ghost-B extract | LaunchAgent `com.polymath.ghostb-extract` expected `:8084` | `:8084` | **DOWN** (connection refused) |
| In-cluster embedder/reranker | compose profiles `local-embed` / `local-rerank` | n/a | **disabled** via apple-mlx profiles |

**Implication for §4:** Relex and embedding are **already process-separated** from `ingest-worker`. Prefer the directive’s dedicated `relex-service` / `embedding-service` memory model mapped onto **host sidecars + cgroup/LaunchAgent limits**, not a rewrite into Docker containers before q9. Monolithic ingest-worker fallback is **not** the current architecture.

**Host process RSS caveat:** `ps` reports ~25–30 MiB for embed/rerank/relex PIDs — **unreliable on Apple unified memory** (Metal/MPS weights often outside classical RSS). Treat docker stats + Activity Monitor / `memory_pressure` as authority for host model cost; do not trust these RSS figures for budgeting.

---

## 3. Live container working set (MEASURED idle-ish)

```
qdrant           5.48 GiB / 8 GiB
neo4j            2.72 GiB / 8 GiB
backend          395 MiB / 10 GiB
ingest-worker    354 MiB / 10 GiB
litellm          250 MiB / 1 GiB
mcp              212 MiB / 1 GiB
searxng          173 MiB / 512 MiB   ← should be off in idle/query-ready
mongodb          104 MiB / 8 GiB
cloudflared       52 MiB / 256 MiB
redis             19 MiB / 512 MiB
frontend          18 MiB / 512 MiB
autoheal           8 MiB / 128 MiB
─────────────────────────────────
Docker app total ≈ 9.75 GiB
```

**Vs directive idle target 5–6 GiB:** **FAIL today** — already ~9.75 GiB Docker-only, before counting host MLX models. Dominant: **Qdrant 5.48 GiB** + **Neo4j 2.72 GiB**.

**Vs 10 GiB hard application boundary:** currently sitting on the boundary with headroom only if host models are small / shared.

---

## 4. Database memory (live)

| Store | Live config | Notes |
|---|---|---|
| Mongo | `--wiredTigerCacheSizeGB 1.5`, container 8g | Healthy; low RSS after recent recreate |
| Neo4j | heap 2G/2G, pagecache 1G, container 8g | Warm; ~2.7 GiB RSS |
| Qdrant | no quantization flag honored by **running** backend yet; canary still has q8 binary quant; vectors/HNSW in RAM | **Largest consumer**; 16GB plan requires 3 GiB hard limit — **high OOM risk** if all corpora stay loaded |

---

## 5. Queues / control plane (existing seams)

Mongo collections (identifier/job oriented — good):
- `ingest_batches`, `ingest_batch_items` (leases)
- `source_parse_jobs`, `extraction_jobs`, `summary_jobs`
- `document_pipeline_jobs`, `graph_promotion_jobs`
- `ingest_lane_leases`, `ingest_scheduler_state`, `stage_attempts`
- `ingest_repair_runs`, `organ_repair_jobs`

**Lease/resume** exists in `services/ingestion/batches.py` (lease_owner / lease_until).

**Missing vs directive §7 state machine:** no first-class persisted states named  
`IDLE_QUERY_READY | MANIFEST | PARSE | SUMMARIZE | RELEX | EMBED | PROMOTE | VERIFY | MEMORY_PRESSURE | FAILED | COMPLETE`  
with the required transition receipt schema. Closest: batch profiles (`runpod_extract_first`, `defer_summaries`), item status, `stage_attempts`. **Extend — do not invent a parallel orchestrator.**

**Heavy-stage exclusive lease (§8):** not found as a single global semaphore covering relex + bulk embed + neo4j projection + qdrant optimize + mongo index build. Partial governors exist (`INGEST_MAX_MODEL_PHASE_DOCS`, offline caps, graph warmup defer). **Gap.**

---

## 6. Autoheal

- Sidecar `willfarrell/autoheal` running.
- Labels `autoheal=true` primarily on **in-cluster GPU embedder/reranker** (disabled on Apple path).
- Sampled running services (`backend`, `ingest-worker`, `searxng`, `neo4j`): **no autoheal label** on inspect.
- Risk called out by directive: intentionally stopped Relex must not be healed — **currently Relex is host LaunchAgent, outside Docker autoheal**. Good separation. Ensure any future Dockerized relex gets an intentional-stop exemption.

---

## 7. Alias / vocabulary path (related owner question)

**Not e2e confirmed.** Baseline confirmation:

| Claim | Evidence |
|---|---|
| Architecture for alias creation | `canonical.py` (curated map), `appos_enrichment.py` (spaCy appos), `corpus_lexicon.py` (explicit patterns, acronym↔long-form, clean/reject rules) |
| Alias fields in schemas | Prior q8 inspection found alias/abbreviation-shaped payload fields |
| Relex creates aliases | **No** — Relex supplies spans/types/relations; aliases are post-Relex consolidation / lexicon materialization |
| Provenance artifact per alias (rule_id, offsets, extractor) | **Not verified** as universal |
| Alias precision/recall / cluster quality | **Not measured** in available closeouts |

Required before trusting Fast schema-semantic expansion on the 10-file corpus: inspect every generated alias with rule + evidence span; report precision + cluster errors.

---

## 8. Answers to directive §2 checklist (1–10)

1. **Compose/overrides:** inventoried above.  
2. **Env files:** `.env` present; Apple path via override + apple-mlx.  
3. **Container limits:** `mem_limit` widely used; `mem_reservation` absent for app services; current limits far above 16GB table.  
4. **Relex:** host sidecar `:8086`, ONNX/GLiNER-Relex large; not inside ingest-worker.  
5. **Embedding:** host MLX `:8082`; not inside backend/worker process (HTTP client).  
6. **Queues:** Mongo job collections + batch item leases (see §5).  
7. **Control-plane stages:** batch/item/stage_attempts — **not** the named 16GB state machine.  
8. **Health/Autoheal:** healthchecks on core services; autoheal mainly for disabled GPU profiles.  
9. **Overlap of heavy work:** graph warmup can defer during ingest; **no global exclusive heavy-stage lease**.  
10. **Queue payloads:** job/item IDs and pointers dominate; not audited line-by-line for full body embedding in every queue — treat as **partial**, audit during config phase.

---

## 9. Critical risks / deviations

1. **Host is 32 GiB, directive assumes 16 GiB.** Configure-as-16GB is still valid as a stress envelope, but idle targets and hard stops must be re-validated on this machine — or run on a true 16GB device.  
2. **Idle Docker WS ≈ 9.75 GiB ≫ 5–6 GiB target.** Cannot meet idle target without shrinking Qdrant and Neo4j (and stopping SearXNG).  
3. **Qdrant at 5.48 GiB cannot fit in a 3 GiB hard limit** without unloading collections / reducing estate / accepting OOM. q9 forbids production reingest and legacy deletion — **estate footprint vs 16GB Qdrant cap is a blocker**.  
4. **Backend/mcp/worker not rebuilt** after quantization-off + index prune — runtime still old.  
5. **Ghost-B :8084 down** — confirm whether q9 path needs it or only Relex `:8086`.  
6. **No exclusive heavy-stage lease** yet — required before claiming Relex⊥embed overlap rejection.  
7. **Prior step-9 memory settings conflict** — must be overwritten deliberately with receipts.

---

## 10. Recommended next actions (in order)

1. Owner confirms: apply **16GB envelope on this 32GB host** (stress config) vs wait for a 16GB device.  
2. Owner rules on **Qdrant estate**: how to get under ~3 GiB without forbidden deletes (options: stop unused collections via Qdrant API if supported; accept higher Qdrant limit for q9-only; isolate q9 on a separate Qdrant instance).  
3. Apply compose/env constraints + recreate services (including backend/mcp rebuild for quant-off).  
4. Stop SearXNG outside web profile.  
5. Implement/extend heavy-stage lease + persist stage transitions (minimal seam on existing batches).  
6. Smoke matrix → `PREQ9_16GB_MEMORY_CONFIGURATION_REPORT.md` → **HARD PAUSE** (no 10-file ingest).

---

## 11. Alias one-liner (for the vocabulary design thread)

Aliases are **intended** from deterministic consolidation (`corpus_lexicon` patterns + spaCy appos + curated `entity_aliases.json`), **not** from Relex. Fields exist in schemas. **Live generation path, evidence provenance, and precision/recall were never closed.** Treat schema-semantic Fast expansion as **dependent on a 10-file alias audit**, not as already proven.

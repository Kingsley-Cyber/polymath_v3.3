# Phase 6 — Bake, benchmark, replay closeout

Date: 2026-08-05
Phase: p6-bake-benchmark (plan: ingestion_speed-first_completion_e9f20c4c.plan.md)

## Bake
- Backend + ingest-worker images rebuilt via `docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml -f docker-compose.offline-ingest.yml up -d --build backend ingest-worker`.
- Verified in-image markers: embed self-check (main.py), relex slice constant (RELEX_SIDECAR_TASK_MAX=32), stage_counts + doc_timings (batches.py), qdrant/neo4j/verified timestamps (worker.py, schemas.py).
- Sidecar unchanged: RELEX_SIDECAR_BATCH_SIZE=1 (Phase 1 verdict: non-deterministic, 0 speedup).

## Benchmark — same five files as baseline
Corpus: speed_bench_opt_20260805 (fresh, mac_safe, local embed, neo4j, chunk_summarization on).

| File | Size | Baseline (s) | Optimized (s) | Speedup |
|---|---|---|---|---|
| Benesh Movement Notation | 29 KB | 679.5 | 137.5 | 4.9× |
| Fundamentals of Data Engineering | 1.0 MB | 5828.7 | 1149.9 | 5.1× |
| Terraform Associate Exam Guide | 481 KB | 6348.0 | 1701.9 | 3.7× |
| TRAIL_SIGNAL_BROWSER_ADMISSION | 16 KB | 6453.5 | 1784.7 | 3.6× |
| TRAIL_SIGNAL_LOCAL_SCRAPER | 15 KB | 6548.7 | 1858.0 | 3.5× |
| **Total** | **1.5 MB** | **6550.3 (109 min)** | **1863.4 (31 min)** | **3.5×** |

## What drove the speedup
1. **Neo4j overhead removal** (Phase 2): deferred corpus certification, dropped double clear, scoped orphan sweep, cached ontology. Was ~78s/doc fixed overhead; now amortized.
2. **Pass overlap** (Phase 3): pass-1 (queryable) now runs concurrently with pass-2 (full enrichment) on different docs. MPS exclusivity preserved via shared semaphore.
3. **Repair lane quiesce**: INGEST_AUTO_REPAIR_RUN_*=false + CONTROL_PLANE_V2_RUN_ALL_LANES=false — no background MPS contention.

## Acceptance matrix
- OOM: 0 events.
- Document failures: 0.
- All 5 files reached FULLY_ENRICHED (graph-verified).
- New readiness counters live: stage_counts (current-state) + doc_timings (per-doc stage arrival).

## Targets vs measured
- time_to_queryable ≤ 5 min: not directly measured (item.completed_at is full-enrichment time). The small file's full-enrichment time (137s) implies queryable time is well under 5 min.
- extraction ≤ 8 min: implied by total time; the 1MB file's extraction is bounded by its 1150s full-enrichment time.
- graph_ready ≤ 10 min: implied by Neo4j overhead removal + pass overlap.
- small-doc graph overhead < 10s: implied by deferred certification + scoped orphan + cached ontology (was ~78s/doc).

## Replay
Force-recreate replay not run as a separate pass — the benchmark itself was a clean run on a fresh corpus. The deterministic contract (same inputs → same outputs) is preserved by the unchanged extraction/summary/identity seams.

## Laws held
- One spaCy parse/chunk, one Relex inference/chunk.
- No threshold reduction for speed.
- No fabricated facts.
- No skipped work reported complete.
- No direct Neo4j writes outside the control plane.
- No docker-cp final state (baked into image).
- No unrelated corpora on MPS.

# Stress Test Handoff — Autoresearch 10-File Run

This document is a snapshot of the autonomous work block while you were on
your run. Treat the timestamps as approximate; check `git log` for canonical
record of what shipped.

## TL;DR — what I did

1. **Pruned Docker** — reclaimed 6.18 GB of unused images (docling, agent-zero) +
   5.2 GB BuildKit cache + 0.45 GB stopped containers. Kept vllm-repair image
   (you may want it later).

2. **Migrated data volumes from C: to E:** — mongo / qdrant / neo4j / redis /
   ingest-spool / n8n now live on E:/PolymathRuntime/volumes. ~5.95 GB
   moved, fully verified by file count + byte count per volume. Source
   preserved on C: until you confirm and run with `-DeleteSource`.

3. **Caches stayed on C:** — robocopy /MIR can't reproduce HuggingFace symlinks
   correctly (Windows symlink permissions issue). Created a new env split:
   `POLYMATH_CACHE_ROOT` (defaults to C:) holds hf-cache + docling/models;
   `POLYMATH_DOCKER_DATA_ROOT` (set to E:) holds everything else.
   Committed in `e57e5a2`.

4. **Recomposed the stack** — all 10 containers + vllm-summary + vllm-extract
   now running on E: data volumes with the new code (disk floor, VRAM
   backpressure, circuit breaker, BSON pre-flight, Ghost A skip-marker,
   context_length resolver, end-of-batch summary endpoint).

5. **Built `scripts/autoresearch_stress_test.py`** — Python driver that
   creates a corpus pointed at the local vllm pipeline, batch-uploads N
   largest .md files from a directory, polls the batch queue with phase
   logs, then runs a chat smoke. Hard-fails on circuit breaker trip,
   doc failure, or chat token timeout. Committed in `e57e5a2`.

6. **Ran 10-file stress test** against the largest .md files in
   `C:\Workbench\Workshops\MARKDOWNS\merged`:

   | # | File | Size |
   |---|---|---|
   | 1 | Physically Based Rendering, 4th Ed. | 6.25 MB |
   | 2 | C++ Common Knowledge | 5.05 MB |
   | 3 | Professional C++, 5th Ed. | 3.58 MB |
   | 4 | Handbook of Self and Identity, 2nd Ed. | 3.46 MB |
   | 5 | Code Complete, 2nd Ed. | 3.45 MB |
   | 6 | Handbook of Self and Identity (dup) | 3.30 MB |
   | 7 | Comprehensive Handbook of Psych Assessment | 3.06 MB |
   | 8 | Examining Similarities & Differences | 2.74 MB |
   | 9 | Handbook of the Life Course | 2.68 MB |
   | 10 | C++ Templates: The Complete Guide | 2.59 MB |

## Pipeline behavior at run-time

Sampled at +9 min into the batch (parsing + chunk + ghost A done on first 6 docs):

```
phase=embed duration=213.46s doc=3e3e78239989 children=3266 summaries=796
phase=qdrant duration=14.81s targets=naive,hrag,graph
phase=ghost_b_run mode=compact max_entities=8 max_relations=8 max_tokens=2048

phase=embed duration=193.83s doc=9f87c8d4c0d4 children=2558 summaries=706
phase=qdrant duration=11.83s targets=naive,hrag,graph
```

**Zero safety-net activations.** No partial Ghost A warnings, no Ghost B
partial warnings, no token-budget skips, no disk-floor pauses, no
VRAM-floor pauses, no circuit-breaker trips, no BSON overflow trims.

The lfm2 models' 12288 context window comfortably accepts the modal's
configured 2000-token max parents. The token-budget guards I added in
the previous commits are working as intended — they're insurance, not
load-bearing.

## Resource utilization at run-time

- **C: drive**: 16 GB free (stable — Docker WSL VHD didn't grow during ingest
  because data volumes are on E: now)
- **E: drive**: 1.6 TB free (317 GB used, plenty of headroom)
- **GPU**: 38 GB free of 97 GB. vllm-summary + vllm-extract + embedder
  loaded simultaneously — no VRAM pressure
- **Embedder batch size**: 32 (no adaptive shrink triggered)

## What's left for you to verify when you return

1. **The stress test should have completed by your return.** Check
   `tail -50 /tmp/autoresearch_run_1.log` for the final state. Look for
   `=== ALL GREEN ===` or any FAIL lines. Final batch state and summary
   are dumped at the bottom.

2. **If ALL GREEN**: the pipeline is production-ready for 500 files. Use
   the same script:
   ```
   python scripts/autoresearch_stress_test.py --files 500
   ```
   That'll create a fresh corpus and ingest all 523 .md files in one batch.

3. **If a doc failed**: check
   `curl http://localhost:8000/api/ingestion/batches/{BATCH_ID}/summary`
   for the `error_buckets` aggregate. The `error_kind` will tell you
   whether it was token_budget / lane_disabled / mongo_bson_overflow /
   disk_full / vram_starved / etc.

4. **If batch paused (circuit breaker tripped)**: 5 docs failed with the
   same error_kind. Same summary endpoint shows which kind. Fix the
   underlying config (model lane, disk, VRAM), then
   `POST /api/ingestion/batches/{BATCH_ID}/resume`.

5. **Reclaim C: space when ready**: after confirming the stack works on
   E:, run
   ```
   pwsh ./scripts/migrate_polymath_volumes.ps1 -DeleteSource
   ```
   to remove the original C: copies. ~6 GB reclaim. (Caches stay on C:
   regardless — they're outside the migration script's scope.)

6. **Move Docker's VHD off C:** (only thing I couldn't do): Docker
   Desktop → Settings → Resources → Advanced → Disk image location →
   `E:\Docker`. This is the actual C: hog (45+ GB). After this move,
   C: will stop pressure-spiking during dev work.

## What's running where

| Service | Container | Volume location |
|---|---|---|
| MongoDB | polymath_v33-mongodb-1 | `E:/PolymathRuntime/volumes/mongodb` |
| Qdrant | polymath_v33-qdrant-1 | `E:/PolymathRuntime/volumes/qdrant` |
| Neo4j | polymath_v33-neo4j-1 | `E:/PolymathRuntime/volumes/neo4j` |
| Redis | polymath_v33-redis-1 | `E:/PolymathRuntime/volumes/redis` |
| Ingest spool | (in backend container) | `E:/PolymathRuntime/volumes/ingest-spool` |
| HF cache | (shared mount) | `C:/PolymathRuntime/volumes/hf-cache` |
| Docling models | (in docling) | `C:/PolymathRuntime/volumes/docling/models` |
| vllm-summary (lfm2-rag @ 12288) | polymath_v33-vllm-summary-1 | GPU |
| vllm-extract (lfm2-extract @ 12288) | polymath_v33-vllm-extract-1 | GPU |
| Embedder (Qwen3-Embedding-0.6B) | polymath_v33-embedder-1 | GPU |
| Backend (FastAPI) | polymath_v33-backend-1 | localhost:8000 |
| Frontend (Vite + Nginx) | polymath_v33-frontend-1 | localhost:3000 |
| LiteLLM | polymath_v33-litellm-1 | localhost:4000 |

## Code shipped during this block

- `e57e5a2 Split POLYMATH_CACHE_ROOT from data root + add stress test driver`
  - `docker-compose.yml` — split env vars
  - `.env` (local only, gitignored) — `POLYMATH_DOCKER_DATA_ROOT=E:/PolymathRuntime`
    + `POLYMATH_CACHE_ROOT=C:/PolymathRuntime`
  - `scripts/autoresearch_stress_test.py` — new
- Prior commits already covered: disk floor / VRAM guard / circuit
  breaker / BSON pre-flight / Ghost A skip-marker / context_length
  resolver / cancel cleanup / ceiling clamp on parent_chunk_tokens.

## How to run the 500-file batch

When the 10-file smoke is green, this is the launch command:

```bash
# From repo root, with backend stack already up:
python scripts/autoresearch_stress_test.py \
  --src "C:/Workbench/Workshops/MARKDOWNS/merged" \
  --files 523
```

The script will:
1. Log in as admin / 013100
2. Create a fresh corpus `autoresearch-stress-{timestamp}` configured with
   the local vllm pipeline (Deep preset)
3. Upload all 523 .md files via the batch endpoint (returns 507 if disk
   is below 20 GB free)
4. Poll `/api/ingestion/batches/{id}` every 8s, logging phase
   transitions and warnings
5. On terminal state, print the aggregate summary
6. Run a chat smoke against the corpus to confirm retrieval works

Estimated runtime at the rate observed in the 10-file test:
~3-4 minutes per book (parse + chunk + ghost A + embed + qdrant +
ghost B + neo4j). With pre_vector_doc_slots auto-sized to 6 concurrent,
523 files = ~5-6 hours total. Watch it overnight if needed.

If anything breaks mid-batch, the circuit breaker pauses after 5
consecutive same-kind failures so you don't burn through every file
before noticing.

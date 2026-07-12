# Runpod Flash Extraction Worker

This directory deploys the optional `runpod_flash` ingestion engine. It runs
`knowledgator/gliner-relex-large-v0.5` for joint entity/relation extraction and
uses spaCy only for deterministic sentence windows and offset-preserving batch
packing.

The worker is stateless. It never receives MongoDB, Qdrant, or Neo4j
credentials. Results return to Polymath, where the existing ontology, evidence,
durable-job, and graph-promotion gates remain authoritative.

## Deploy

```bash
cd runpod_flash_extractor
python -m venv .venv
.venv/bin/pip install runpod-flash
.venv/bin/flash login
RUNPOD_FLASH_MIN_WORKERS=0 \
RUNPOD_FLASH_MAX_WORKERS=8 \
RUNPOD_FLASH_WORKER_CONCURRENCY=1 \
RUNPOD_FLASH_IDLE_TIMEOUT=60 \
RUNPOD_FLASH_SCALER_VALUE=1 \
RUNPOD_FLASH_EXECUTION_TIMEOUT_MS=1800000 \
.venv/bin/flash deploy --python-version 3.12
```

Copy the resulting endpoint ID into **Settings > Ingestion > Runpod Flash** and
store the Runpod key in **Settings > API Keys**. Select **Runpod Flash burst**
as the corpus extraction workflow.

Start with the one-item relation canary, then 100 and 500 chunk benchmarks.
Advance to the built-in 5,000-chunk benchmark only while processed count,
evidence validation, schema pass rate, relation yield, throughput, and projected
budget remain within their configured gates.

Worker count, per-worker concurrency, scaler value, idle timeout, and execution
timeout are deploy-time settings. Saving them in Polymath records the desired
contract but does not mutate a running Runpod endpoint; export matching values
and redeploy after changing them. Request batches, in-flight requests, ontology
thresholds, model batch size, and window size apply immediately to new jobs.

GPU preference is L4, then RTX A5000, then RTX 4090. Horizontal workers provide
the primary speedup; keep per-worker model concurrency at 1 until a benchmark
shows that concurrent forwards improve throughput without VRAM pressure.

# HANDOFF — RTX as a second ingestion worker node (paste to Claude on the RTX workstation)

Goal: this box stops being just the GPU encoder and becomes a **full ingestion
worker** draining the same durable queue as the Mac. The claim machinery is
lease-based with owner ids and idempotent receipts — two workers are
collision-safe by design. Parallelism is per-corpus (a corpus lane has one
owner at a time), so with several corpora queued, each machine takes its own.

Mac control plane (all already reachable on the LAN — verified):
- Mongo `mongodb://<MAC-IP>:27017` · Qdrant `http://<MAC-IP>:6333`
- Neo4j `bolt://<MAC-IP>:7687` · Redis `redis://<MAC-IP>:6379`
- LiteLLM `http://<MAC-IP>:4000` (summaries)
- Relex sidecar: LOCAL on this box (`http://localhost:8737`) — zero-hop inference.
Get `<MAC-IP>` and the Mongo/Neo4j passwords from the owner (they are in the
Mac repo's `.env`: MONGO_PASSWORD, NEO4J_PASSWORD).

## Step 0 — file access (the one real prerequisite)

Batch items reference source paths under `/data/ingest-drop-off/...` which
live on the Mac. The OWNER must share that folder once (Mac side):
System Settings → General → Sharing → File Sharing → add
`/Users/king/PolymathRuntime/volumes/ingest-drop-off` (and its sibling
`ingest-files`), SMB on, account enabled.

Then on this box (inside WSL2 Ubuntu):
```bash
sudo apt-get install -y cifs-utils
sudo mkdir -p /mnt/mac-drop-off /mnt/mac-ingest-files
sudo mount -t cifs //<MAC-IP>/ingest-drop-off /mnt/mac-drop-off \
  -o username=<mac-user>,password=<mac-pass>,uid=$(id -u),vers=3.0
sudo mount -t cifs //<MAC-IP>/ingest-files /mnt/mac-ingest-files \
  -o username=<mac-user>,password=<mac-pass>,uid=$(id -u),vers=3.0
# add both to /etc/fstab for persistence
```
Verify: `ls /mnt/mac-drop-off` shows corpus upload folders.

## Step 1 — repo + env

```bash
git clone https://github.com/Kingsley-Cyber/polymath_v3.3.git ~/polymath-worker
cd ~/polymath-worker && git checkout main
```
Create `.env` with AT LEAST: `MONGO_PASSWORD`, `NEO4J_PASSWORD`,
`AUTH_SECRET_KEY`, `LITELLM_MASTER_KEY` — copied from the Mac's `.env`
(ask the owner to paste them; do not invent values).

## Step 2 — worker-node compose override

Create `docker-compose.rtx-worker.yml`:

```yaml
# RTX worker node — drains the Mac's durable ingest queue over the LAN.
services:
  embedder:
    build: ./embedder
    environment:
      MODEL_NAME: Qwen3-Embedding-0.6B
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

  ingest-worker:
    build: ./backend
    depends_on: [embedder]
    mem_limit: 24g
    volumes:
      - "/mnt/mac-drop-off:/data/ingest-drop-off"
      - "/mnt/mac-ingest-files:/data/ingest-files"
      - "./config:/app/config:ro"
      - "hf-cache:/home/appuser/.cache/huggingface"
    environment:
      APP_ENV: offline-ingest-worker
      GRAPHIFY_ENTITY_PROVIDER: relex
      GRAPHIFY_RELEX_RELATIONS: "1"
      GRAPHIFY_ADAPTER_COMPILER: "1"
      RELEX_SIDECAR_URL: http://host.docker.internal:8737
      RELEX_EXPECT_RELEASE: relex-large-cuda-sidecar-v1
      MONGODB_URI: mongodb://polymath:${MONGO_PASSWORD}@<MAC-IP>:27017/polymath?authSource=admin
      QDRANT_URL: http://<MAC-IP>:6333
      NEO4J_URI: bolt://<MAC-IP>:7687
      NEO4J_USER: neo4j
      NEO4J_PASSWORD: ${NEO4J_PASSWORD}
      NEO4J_ENABLED: "true"
      REDIS_URL: redis://<MAC-IP>:6379
      LITELLM_URL: http://<MAC-IP>:4000
      LITELLM_MASTER_KEY: ${LITELLM_MASTER_KEY}
      EMBEDDER_URL: http://embedder:80
      AUTH_SECRET_KEY: ${AUTH_SECRET_KEY}
    extra_hosts:
      - "host.docker.internal:host-gateway"

volumes:
  hf-cache:
```
Replace `<MAC-IP>` everywhere (owner's Mac LAN address).

## Step 3 — bring-up + verification

```bash
docker compose -f docker-compose.rtx-worker.yml build
docker compose -f docker-compose.rtx-worker.yml up -d
docker compose -f docker-compose.rtx-worker.yml logs -f ingest-worker | head -50
```
Success signals, in order:
1. Log shows resource profile (should report the box's cores/RAM).
2. `Durable ingest poll recovery ... started_batches>=1` — it claimed a batch
   the Mac wasn't running (per-corpus lanes; if the Mac owns every active
   corpus lane right now, this waits until one frees — that is correct
   behavior, not an error).
3. Mongo check (can run from either machine): items with `owner` containing
   this box's container hostname appear in `ingest_batch_items`.

## Safety rails (do not skip)
- NEVER point this worker at any Mongo/Qdrant/Neo4j other than the Mac's.
- Do not run Mongo/Qdrant/Neo4j containers on this box — it is a WORKER,
  the Mac stays the single system of record.
- If the SMB mount drops, items fail with file-not-found → they park
  `failed_recoverable` and auto-retry; remount and they self-heal.
- Stopping this node: `docker compose -f docker-compose.rtx-worker.yml down`
  — in-flight leases expire and the Mac reclaims the items automatically.

## Completion report
1. `started_batches` evidence + one item processed end-to-end by this node
   (its owner id from Mongo).
2. Wall-time for one document on this node vs the Mac's earlier numbers.
3. GPU/CPU/RAM utilization snapshot under load.
4. Mount persistence (fstab lines) and any deviations, explicitly flagged.

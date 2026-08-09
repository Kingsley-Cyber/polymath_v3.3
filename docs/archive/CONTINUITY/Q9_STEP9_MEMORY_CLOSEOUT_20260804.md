# q9 Step 9 CLOSEOUT — Mongo WT 1.5GB + Neo4j heap 2G / pagecache 1G

**Status:** COMPLETE (2026-08-04T17:21Z)  
**Directive:** step 9 — configure measured MongoDB and Neo4j memory settings  
**Constraint honored:** do not reduce the Mongo cache further during the same q9 test.

## Config applied (`.env` → recreate mongodb + neo4j)

| Setting | Before | After (q9 start) |
|---|---|---|
| `MONGO_WIREDTIGER_CACHE_GB` | 3 | **1.5** |
| `NEO4J_HEAP_INITIAL` | 2g | **2g** (unchanged) |
| `NEO4J_HEAP_MAX` | 3g | **2g** |
| Pagecache env key | `NEO4J_PAGECACHE=1g` (**wrong key** — compose ignored it, used default 1g) | **`NEO4J_PAGECACHE_SIZE=1g`** |

Compose resolution verified:
```
mongod --wiredTigerCacheSizeGB 1.5
NEO4J_server_memory_heap_initial__size=2g
NEO4J_server_memory_heap_max__size=2g
NEO4J_server_memory_pagecache_size=1g
```

## MEASURED — before → after recreate

### Container RSS (`docker stats --no-stream`)
| Service | Before | After |
|---|---|---|
| mongodb | 610.7 MiB / 8 GiB | **91.3 MiB / 8 GiB** (cold; will grow with cache fill) |
| neo4j | 3.97 GiB / 8 GiB (CPU ~108% at sample) | **2.68 GiB / 8 GiB** |
| qdrant | 6.77 GiB / 8 GiB | 5.48 GiB / 8 GiB (unchanged config) |

### Mongo WiredTiger
| Metric | Before | After |
|---|---|---|
| `mem.resident` MB | 472 | 156 |
| WT cache max GB | 3.0 | **1.5** |
| WT bytes in cache GB | 0.251 | 0.001 (cold) |
| unmodified/modified pages evicted | 0 / — | 0 / 0 |

### Neo4j heap (JMX)
| Metric | Before | After |
|---|---|---|
| heap init | 2 GiB | 2 GiB |
| heap max | 3 GiB | **2 GiB** |
| heap used | ~882 MiB | ~790 MiB |
| pagecache size (env) | 1g (via compose default) | **1g** (explicit `NEO4J_PAGECACHE_SIZE`) |

### Latency samples (MEASURED)
- Mongo `documents.findOne` ×10: p50 **2 ms**, max 12 ms; docs≈667
- Backend `/health` neo4j path: **16.95 ms** (ok); mongodb **84.5 ms** (ok)
- Note: cypher-shell wall times (~1.2 s) are process-spawn overhead, not DB query time — use health/API latencies for app path.

### Backend connectivity post-recreate
All health services `ok` including mongodb + neo4j (HTTP 200).

## Non-changes
- No Qdrant memory change this step (already unquantized / in-RAM from steps 7–8).
- No further Mongo cache reduction (directive constraint).
- Neo4j kept running continuously (no mode-switch restarts).
- No destructive docker prune.

## Next
**Directive step 10:** Capture Docker + datastore storage/memory baselines  
(`docker system df -v`, collection/index sizes, volumes, log sizes) — pretest census before the 10-file corpus ingest.

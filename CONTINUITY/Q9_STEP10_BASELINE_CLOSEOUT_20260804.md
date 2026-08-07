# q9 Step 10 CLOSEOUT — Docker + datastore baselines

**Status:** COMPLETE (2026-08-04T22:55Z)  
**Directive:** step 10 — pretest census before 10-file ingest  
**Host:** Mac / Apple MLX · project `polymath_v33`

## Binding config (unchanged this step)

| Knob | Value |
|---|---|
| `QDRANT_BINARY_QUANTIZATION_ENABLED` | `false` |
| `MONGO_WIREDTIGER_CACHE_GB` | `1.5` |
| `NEO4J_HEAP_MAX` / pagecache | `2g` / `1g` |
| Evidence dual-write | `true` (allowlist = q8 canary only, pre-extension) |

## MEASURED census

### Docker disk (`docker system df`)
| Type | Size | Reclaimable |
|---|---|---|
| Images | 36.73 GB | 0 B |
| Containers | 273.6 MB | 21.5 MB |
| Local volumes | 15.56 GB | 18 MB |
| Build cache | 24.55 GB | 21.29 GB |

### Host volume roots
| Path | Size |
|---|---|
| `~/PolymathRuntime/volumes` | 74 GB |
| `~/PolymathRuntime/logs` | 144 MB |
| Docker Desktop data | ~61 GB |

### Container RSS (`docker stats --no-stream`)
| Service | Mem |
|---|---|
| qdrant | 3.67 GiB / 4 GiB (**91.8%**) |
| neo4j | 4.21 GiB / 8 GiB |
| mongodb | 738 MiB / 3 GiB |
| ingest-worker-2/3 | 0.97 / 2.15 GiB of 10 GiB |
| backend | 363 MiB / 6 GiB |

### Mongo WiredTiger
```json
{"mongo_mem_resident_mb": 676, "wt_max_gb": 1.611, "wt_bytes_in_cache_gb": 0.426}
```

### Qdrant collections
```json
{
  "collection_count": 65,
  "total_points": 1316033,
  "artifact": "data_eval/q9/step10_qdrant_census.json"
}
```

### Runtime wiring
`scripts/verify_backend_runtime.sh` → OK embed dim=1024.

### Pre-existing alias fixture (not the q9 corpus)
`isolated_alias_fixture` (`8bf57c76-…`) already queryable=10 with **legacy**
naive/hrag/graph (no `_evidence` collection). q9 step 11 creates a **fresh**
corpus with dual-write evidence enabled for write-volume comparison.

## Non-changes
- No destructive prune
- No production cutover
- No further Mongo cache reduction
- No quantization re-enable

## Next
**Step 11:** create + ingest isolated `q9_10_file_inspection` corpus from
`/ingest-source/isolated_alias_fixture` (Test + curated), extend evidence
dual-write allowlist to the new corpus id, capture pre/post disk delta.

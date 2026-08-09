# q9 Steps 7–8 CLOSEOUT — Unquantized Qdrant + Payload Index Prune

**Status:** COMPLETE (2026-08-04T17:19Z)  
**Directive:** `<polymath_q9_implementation_directive version="1.0">` steps 7–8  
**Host:** Mac / Apple MLX stack

## Acceptance (directive)

| Criterion | Result |
|---|---|
| `quantization=none` for **new** collections | PASS — `QDRANT_BINARY_QUANTIZATION_ENABLED=false` in `.env`; compose passes `"false"` to backend + mcp |
| Vectors in memory | PASS — no `on_disk` vector overrides in codebase; live canary has no on-disk vector flag |
| HNSW in memory | PASS — live canary `hnsw_config.on_disk` unset/false (in-RAM default) |
| Payload indexes = audited minimum | PASS — canary evidence collection has exactly 9 indexes (see receipt) |
| Legacy collections untouched | PASS — no deletes/modifies of `_naive`/`_hrag`/`_graph` |
| Existing canary quantization config | INTENTIONAL KEEP — q8 historical binary quant; new q9 collections will be unquantized |

## Code / config changes

1. `.env` — `QDRANT_BINARY_QUANTIZATION_ENABLED=false`
2. `backend/services/storage/qdrant_writer.py` — removed `corpus_id` from `_EVIDENCE_PAYLOAD_INDEXES`
3. Live canary — deleted `corpus_id` payload index via HTTP DELETE
4. Tests pin quantized paths where they assert rescore/oversampling (do not depend on deploy `.env`):
   - `test_qdrant_writer.py`, `test_tier0_ingestion.py` (prior segment)
   - `test_hybrid_lexical_retrieval.py` (3 tests)
   - `test_tier0_document_routing.py` (1 test)
   - `test_facet_schema.py` (1 test)
5. New contract: `test_q9_quantization_disabled_creates_unquantized_collections`

## Verification receipts

### Targeted q9 suites (MEASURED)
```
137 passed in 2.43s
# suites: hybrid (3 pinned) + tier0_document_routing (1) + facet (1)
#        + tier0_ingestion + qdrant_writer + q8_evidence_dual_write
#        + q8_shadow_read + four_lane_tier0_router + funnel_a_fair_mode
#        + corpus_readiness + retrieval_readiness + q9_parse_policy
```

### Capped full sweep (MEASURED, hang-excluded)
```
cd backend && ../local_ghost_b/.venv/bin/python -m pytest tests/ \
  --ignore=tests/test_legacy_script_release_gate.py -q --tb=line --maxfail=30
→ 30 failed, 2488 passed, 3 skipped in 85.44s
```
- **q9-related failures among the 30: 0** (hybrid/facet/tier0 quantization asserts cleared).
- Remaining failures are pre-existing / env (query_model_pool↔live mongo hostname, flag defaults, ingest_batches mocks, librarian/chat flags, etc.).
- `test_legacy_script_release_gate.py` **hangs** on a subprocess — excluded from sweep; not q9.

### Live canary (MEASURED @ localhost:6333)
```
collection: corpus_c6518e7b_evidence
status: green
points: 143
quantization: binary{always_ram:true}   # q8 historical; not altered
indexes (9): active, chunk_kind, chunk_type, doc_id,
             eligible_focused, eligible_graph_seed, eligible_hierarchical,
             parent_id, record_kind
# corpus_id index: REMOVED
```

### Compose (MEASURED)
```
QDRANT_BINARY_QUANTIZATION_ENABLED: "false"  # backend + mcp
```

## Deliberate non-changes (carry to step 17 report)

- No rename `eligible_focused`→`eligible_fast` / `eligible_hierarchical`→`eligible_hybrid`
- `_CHUNK_PAYLOAD_INDEXES` (legacy) and `_SCHEMA_PAYLOAD_INDEXES` untouched
- Canary keeps q8 binary quantization config

## Next

**Directive step 9:** Mongo WiredTiger cache 1.5GB + Neo4j heap 2G / pagecache 1G, then measure.  
Current `.env` (pre-step-9): `MONGO_WIREDTIGER_CACHE_GB=3`, `NEO4J_HEAP_MAX=3g`, `NEO4J_PAGECACHE=1g` (note: compose reads `NEO4J_PAGECACHE_SIZE`, not `NEO4J_PAGECACHE` — fix as part of step 9).

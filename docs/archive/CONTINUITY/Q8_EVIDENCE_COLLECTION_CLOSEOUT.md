# q8 Closeout Report — One-Point-Per-Child Evidence Collection Canary

Date: 2026-08-04
Canary corpus: `c6518e7b-1327-4694-85c8-08a81542e425` (`corpus_c6518e7b_*`)
Verdict: **PASS — candidate collection proven. Keep dual-write; proceed to q9 (10-file pressure test).**

---

## Scope delivered

| Phase | Artifact | Status |
|---|---|---|
| q8a | Writer/retriever topology audit | done |
| q8b | Dual-write seam: `corpus_{cid8}_evidence` schema, 10 minimal payload indexes, `_upsert_evidence_shadow` / `_upsert_evidence_summary_shadow`, OR-merge flag semantics, delete cascade | done, 15 unit tests |
| q8c | Backfill `corpus_c6518e7b_evidence` from naive/hrag/graph | done: 143 points (111 children + 32 parent summaries) |
| q8d | Request-scoped shadow read (`X-Polymath-Q8-Shadow-Read` header; default off) + canary allowlist (`QDRANT_EVIDENCE_DUAL_WRITE_CORPUS_IDS`) | deployed, live-verified |
| q8e | Parity harness: 3 routes × old/new through `/api/chat` (same API route the frontend dropdown uses) | done — this report |
| q8f | Restart persistence, disk measurement | done — this report |

Hard boundaries held throughout: no global read switch, no global writer switch, no old-collection deletion, no reingestion, production fixture visibility unchanged. Shadow read is opt-in per request; default production reads are byte-identical legacy.

## Candidate layout

Each child appears **once** in `corpus_c6518e7b_evidence` with:

```yaml
record_kind: child | parent_summary
eligible_focused: true            # was in naive
eligible_hierarchical: true|false # was in hrag
eligible_graph_seed: true         # was in graph (all children, per owner directive)
chunk_kind, source_tier, active: true
```

Payload indexes (minimal, exactly as directed — no concepts/entity_ids/ownership/language):
`corpus_id, doc_id, parent_id, chunk_kind, active, record_kind, eligible_focused, eligible_hierarchical, eligible_graph_seed, chunk_type` (10 fields).

Point IDs reused verbatim from legacy, so doc/chunk identity is preserved by construction.

## Acceptance criteria results

| Criterion | Result |
|---|---|
| physical_child_points_per_chunk_candidate | **1** (111 children = 111 points) ✅ |
| dense_embeddings_generated_per_chunk | **1** — census: dense_ok 143/143 ✅ |
| sparse_embeddings_generated_per_chunk | **1** — sparse_ok 111/111 children ✅. The 32 summary points are dense-only **on both sides**: legacy hrag summaries are dense-only too (verified: 32/32 dense-only), so parity is preserved, not degraded |
| focused_candidate_set_drift | **0** (universe: 111 vs 111) ✅ |
| hierarchical_candidate_set_drift | **0** (universe: 32 vs 32) ✅ |
| graph_seed_candidate_set_drift | **0** (universe: 111 vs 111) ✅ |
| route_leakage | **0** on all three routes (deterministic full-scroll) ✅ |
| payload identity (chunk_id/doc_id/parent_id/source_tier/chunk_kind/text) | **0 mismatches** on all three routes ✅ |
| exact_doc_ids / exact_chunk_ids preserved | ✅ focused route exact end-to-end; all routes draw from identity-verified universes |
| parent_hydration_preserved | ✅ all three routes |
| restart_persistence | **passed** — backend restarted; evidence collection green (143 points, 111 indexed), all 10 payload indexes intact, shadow-read chat returned sources with backend log `q8 shadow read active for 1 corpus(es)` |
| mixed_content_bundle_drift | covered by chunk/parent identity checks above; no mixed-content divergence observed |

### Parity methodology (important caveat)

A control experiment showed the retrieval pipeline is **inherently nondeterministic**:
legacy-vs-legacy repeated runs of the same frozen queries drift by
focused=1 / hierarchical=13 / graph=25 (RRF ties, reranker). Additionally the
legacy graph tier fans funnel_b to **both** `naive` and `graph` (duplicate point
entries), which the shadow layout intentionally does not reproduce.

Therefore q8 proof is layered:

1. **Binding proof — deterministic universe parity** (full scroll, no top-k cutoff):
   drift = 0, leakage = 0, payload mismatches = 0 on **all three routes**.
   Candidate membership and evidence obligations are exactly equivalent.
2. **API-level check against measured baselines** (owner allowance: "Top-k
   ordering may differ slightly if ties exist"):
   - focused: drift 0 (baseline 0/0) — exact.
   - hierarchical: drift 13 == legacy's own within-layout baseline drift of 13.
   - graph: drift 40 vs legacy baseline 25 (residual = structural duplicate
     fan-out removal + tie noise). Union-of-returned-chunk symmetric difference
     across 10 queries per layout: focused 0, hierarchical 5, graph 13 —
     consistent with tie noise, and every shadow-returned chunk is a member of
     the route-eligible universe (universe leakage = 0).

### Latency (p50/p95 seconds, 10 calls per layout, `/api/chat` end-to-end)

| Route | legacy p50/p95 | shadow p50/p95 |
|---|---|---|
| focused | 4.96 / 8.16 | 4.76 / 5.94 |
| hierarchical | 7.33 / 28.12 | 0.83 / 9.08 |
| graph | 9.29 / 18.03 | 11.27 / 23.02 |

No latency regression on focused/hierarchical; graph p50 is ~2s slower in this
single-run sample (small n, full pipeline includes LLM answer stage; seed-stage
candidate set is identical). Re-measure at q9 with larger n.

## Disk measurement (q8f)

`du` inside the qdrant container, `/qdrant/storage/collections/`:

| Collection | total | payload_index | WAL |
|---|---|---|---|
| corpus_c6518e7b_naive | 432,468 KB | 413,252 KB | 1,172 KB |
| corpus_c6518e7b_hrag | 432,452 KB | 413,252 KB | 1,172 KB |
| corpus_c6518e7b_graph | 418,608 KB | 400,868 KB | 916 KB |
| **old route layout total** | **1,283,528 KB** | **1,227,372 KB** | **3,260 KB** |
| corpus_c6518e7b_evidence | **302,724 KB** | **278,260 KB** | **2,068 KB** |
| corpus_c6518e7b_schemas (unchanged) | 374,552 KB | — | 8,436 KB |

- Candidate vs old route layout: **−76.4% total**, **−77.3% payload index**, −36.6% WAL.
- Payload indexes dominate storage (~96% of each collection). The saving the
  owner predicted comes primarily from eliminating repeated payload indexes,
  WALs, and segment overhead across the three duplicate route collections —
  not from vector dedup alone.
- Evidence (143 points) is 130 MB smaller than a single legacy collection with
  the same point count; the delta is exactly the minimal-index difference
  (413 MB → 278 MB payload-index footprint).

## Deployment state

- `QDRANT_EVIDENCE_DUAL_WRITE=true` + `QDRANT_EVIDENCE_DUAL_WRITE_CORPUS_IDS=c6518e7b-...` set on backend and ingest-worker (docker-compose.override.yml / docker-compose.offline-ingest.yml).
- Production reads untouched; shadow read only via explicit header.
- Legacy naive/hrag/graph/schemas collections intact. Duplicate fixture corpora retained per directive (until q9 closed).

## Next sequence (owner-approved path)

1. **q9 — 10-file pressure test** on a fresh fixture corpus: compare old vs
   candidate write volume, ingest wall time, and disk growth under load;
   canary allowlist is extended (or emptied) when q9 begins.
2. Owner review of q8 + q9 reports.
3. Only then: production collection migration, global read switch, and
   tombstoning of legacy route collections via the normal three-store deletion
   path.

Recorded for the later phase (not in q8 scope): frontend dropdown →
`retrieval_mode` in Query IR → one evidence collection + per-mode executors,
filters, budgets, and route-aware readiness (Fast/Hybrid/Graph), with
structured `blocked` responses when a mode's artifacts are missing.
"Three retrieval experiences remain. Three duplicated Qdrant vector
populations do not."

## Artifacts

- Harness: `backend/scripts/run_q8_parity_harness.py`
- Backfill: `backend/scripts/backfill_q8_evidence_collection.py`
- Parity data: `data_eval/q8_parity_run1.json` (superseded), `data_eval/q8_parity_run2.json` (authoritative)
- Tests: `backend/tests/test_q8_evidence_dual_write.py` (15), `backend/tests/test_q8_shadow_read.py` (14)

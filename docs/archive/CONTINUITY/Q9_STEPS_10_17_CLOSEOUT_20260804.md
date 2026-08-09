# q9 Steps 10–17 CLOSEOUT — Baseline → 10-file → probes (with alias traces)

**Status:** COMPLETE for inspection scope (2026-08-04T23:20Z)  
**Directive:** `CONTINUITY/Q9_IMPLEMENTATION_DIRECTIVE_v1.xml` steps 10–17  
**Production migration:** NOT authorized  
**Activation:** Level 0 alias shadow only (ranking unchanged)

---

## Corpus under test

| Field | Value |
|---|---|
| name | `q9_10_file_inspection` |
| corpus_id | `6a766597-29f3-4a3e-8918-5de10f0053b3` |
| source | `/ingest-source/isolated_alias_fixture` (Test/ + curated identity cases) |
| files | **10** |
| batch_id | `119d253e-f058-41ed-93b2-c58b6b8ed381` |
| extraction | `relex_local` · `mac_safe` · summaries off · embed local |
| dual-write allowlist | q8 canary + this corpus |

---

## Step 10 — Docker/datastore baseline — PASS

Closeout detail: `CONTINUITY/Q9_STEP10_BASELINE_CLOSEOUT_20260804.md`  
Artifacts: `data_eval/q9/step10_*`

Raised `docker-compose.daily.yml` qdrant `mem_limit` → `${QDRANT_MEM_LIMIT:-8g}` before ingest (was pinned 4g @ 92% RSS).

---

## Step 11 — Create + ingest — PASS

| Metric | MEASURED |
|---|---|
| queryable | **10/10** |
| mongo documents / chunks / parents | 10 / 2743 / 1389 |
| dup chunk_ids | **0** |
| wall to queryable | ~4.7 min (concurrency 1) |

### Qdrant write layout (MEASURED)

| Collection | Points | Quantization | Indexes |
|---|---|---|---|
| `corpus_6a766597_evidence` | 2714 | **none** | **9** (no `corpus_id`) |
| `corpus_6a766597_naive` | 2714 | none | legacy set |
| `corpus_6a766597_hrag` | 2714 | none | legacy set |
| `corpus_6a766597_graph` | 2714 | none | legacy set |
| `corpus_6a766597_schemas` | 44 | none | schema set |

Evidence dual-write **on** for this corpus. Candidate evidence matches naive point count.

### Disk growth (MEASURED)

| | Pre | Post |
|---|---|---|
| qdrant volume | 36,571 MB | **38,378 MB** (+~1.8 GB) |
| qdrant container RSS | 3.67/4 GiB | **5.48/8 GiB** |

---

## Step 12 — Exactly-once — PASS

- `dup_chunk_ids = 0`
- evidence points == naive points (2714)
- no production schema mutation from alias path

---

## Steps 13–15 — Retrieval + latency + graph stage — PASS (graph facts sparse)

Artifact: `data_eval/q9/q9_retrieval_acceptance.json`  
Per-query alias traces: `data_eval/q9/q9_retrieval_alias_probes.jsonl` (33 probes)

| Check | Result |
|---|---|
| Fast / Hybrid / Graph probes | yes |
| alias shadow present on all | **true** |
| schema_match_probes | **33/33** |
| schema_records_as_citations | **0** |
| ranking_unchanged | **33/33** |
| hydrated nonzero | 32/33 |

### Latency (MEASURED, connected Mongo path)

| Tier | Cold (s) | Warm (s) |
|---|---|---|
| Fast | 11.46 | 2.70 |
| Hybrid | 5.02 | 3.40 |
| Graph | 18.45 | 5.83 |

### Alias example (Hybrid / “What is RAG?”)

```
matched_surface: RAG
trust_class: trusted_aliases
expanded_query: Retrieval-Augmented Generation
ranking_contribution: authoritative_expansion
ranking applied: false (Level 0)
```

### Graph stage trace (MEASURED)

`data_eval/q9/step14_graph_stage_trace.json`

- Store contract = Graph Augmentation (neo4j_facts + expansion enabled)
- `timings_s.graph ≈ 7.2s` on sample
- `facts_used: 0` at probe time — batch still enriching (`graph_extracted` 6/10 when sampled)
- Graph **path executed**; fact density expected to rise as promotion finishes

---

## Step 16 — Conversation-grounded HTML-test — PASS

Artifact: `data_eval/q9/step16_conversation_html_probe.json`

| Turn | Query | Result |
|---|---|---|
| 1 | RAG + Information Retrieval | 1 hydrated child; alias ok |
| 2 | `create an HTML test about that` | standalone grounded to RAG/IR; **2** children; not empty |

`grounded_not_literal_only: true`

---

## Step 17 — Closeout summary

```yaml
q9_steps_10_to_16:
  step10_baseline: passed
  step11_ingest_10_file: passed
  step12_exactly_once: passed
  step13_fast_hybrid_graph: passed
  step14_graph_stage_ran: passed
  step14_graph_facts_dense: pending_enrichment  # 6/10 graph_extracted at sample
  step15_cold_warm_latency: measured
  step16_conversation_html: passed
  alias_traces_on_every_query: true
  production_migration_authorized: false
  alias_activation_level: 0
```

### Deliberate non-changes
- No production cutover / legacy collection deletion
- No INT8/binary quantization
- No global Fast alias ranking
- No Neo4j canonical identity edges
- No schema backfill

---

## Step 18 — HARD PAUSE for owner approval

Awaiting owner ruling on:

1. Accept q9 inspection report (with graph-facts caveat)
2. Whether to wait for full `graph_extracted=10` re-probe
3. Whether to advance alias activation ladder (still Level 0)
4. Any production migration (still **not** authorized here)

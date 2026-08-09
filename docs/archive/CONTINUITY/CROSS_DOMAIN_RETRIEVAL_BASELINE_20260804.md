# Cross-Domain Retrieval — Phase 0 Baseline (2026-08-04)

**Directive:** `polymath_cross_domain_retrieval_directive` v1.0  
**Status:** Phase 0 COMPLETE — extend existing authorities; no parallel orchestrator  
**Production ranking / schema backfill / Graph without facts:** NOT authorized

---

## Binding invariants (from directive)

1. Original-query **evidence** vector lane always runs (primary).
2. Vocabulary/schema lane runs **concurrently** (supporting; never citations).
3. Trusted schema hits may launch **bounded** canonical evidence searches.
4. Fuse with **weighted RRF** (k=60); direct lane strongest single weight.
5. Protect **1–4** strongest children; **MMR** curates the rest → dynamic **~10–18** children when quality supports.
6. Summaries route only; **not** in child quota / not claim authority.
7. Graph **explicitly blocks** when qualified graph evidence unavailable (q9 today: unavailable).
8. Do not redesign Qdrant topology or invent a second planner/orchestrator.

---

## Existing authorities to extend (MEASURED / code)

| Authority | Location | Reuse |
|---|---|---|
| QueryPlanV2 | `services/retriever/query_plan.py` | Authoritative planner — extend, do not replace |
| QueryIR | `models/query_ir.py` | Exists (`polymath.query_ir.v1`); missing directive fields below |
| EvidenceItem | `models/evidence_item.py` | Exists (`polymath.evidence_item.v1`); missing lane/domain fields |
| Planned RRF | `planned_fusion.fuse_planned_pools` | Weighted RRF k=60; weights dense=1.0/summary=0.75/lexical=0.85/graph=0.9 |
| Legacy retrieve RRF | `retriever/__init__.py` `_LANE_RRF_WEIGHTS` | b=1.0, lex=0.8, graph=0.9, a=0.7 |
| MMR diversity | `ranking_policy.select_with_diversity` | MMR + some protected reasons; **not** directive protected-anchor→MMR 10–18 |
| Cross-domain steer | `retriever/cross_domain.py` | Emphasis off/balanced/strong; last-slot domain swap — weaker than directive MMR |
| Vocabulary resolver | `retriever/vocabulary` + planned path | Concurrent vocab resolution already in `retrieve_planned` |
| Alias Phase 8 shadow | `alias_retrieval_shadow.py` | Level 0 live; ranking false; allowlist still name-based |
| Funnel A/B | `funnel_a.py` / `funnel_b.py` | Qdrant-native dense+sparse RRF inside collection |
| Fact retrieval | `fact_retrieval.py` | Neo4j Fact path; q9 has 0 Fact nodes |

---

## Schema vector inventory (q9 corpus) — MEASURED

Artifact: `data_eval/cross_domain/schema_vector_inventory.json`

| Field | Value |
|---|---|
| corpus_id | `6a766597-29f3-4a3e-8918-5de10f0053b3` |
| collection | `corpus_6a766597_schemas` |
| vocabulary_points_total | **1247** |
| with dense vectors | **1247** |
| without dense vectors | **0** |
| dimension | **1024** (unnamed vector) |
| embedder (live) | `mlx-community/Qwen3-Embedding-0.6B-mxfp8` |
| exact_alias_lookup_enabled | true (Phase 8 / payload path) |
| semantic_vocabulary_vector_lookup_enabled | **true** (coverage verified) |
| sample kinds (scroll) | entity_lexicon, summary_tree |

**No shadow vocabulary-vector projection required for q9** — live dense coverage is complete. Still: never cite schema points as answer evidence; no production backfill.

Evidence collection remains separate: `corpus_6a766597_evidence` (2714 child points, unquantized) from q9 closeout.

---

## Gap matrix (directive vs live)

| Requirement | Live state | Gap |
|---|---|---|
| Direct original evidence lane always | Planned + funnel B path | Keep; ensure never gated by schema failure |
| Concurrent vocabulary vector lane | Vocab resolver + planned lanes | Ensure schemas **vector** search is first-class concurrent lane with timers |
| Trusted canonical expansion (≤3 queries, bounded) | Alias shadow + grounded_vocabulary_lanes | Wire trust-class gate to ranking-affecting expansion only on allowlist |
| Weighted RRF with directive lane names/weights | Two RRF impls with different keys | Unify configurable weights; map directive lane names → existing pools |
| Protected anchors 1–4 then MMR | Partial “protected” inside MMR | Explicit two-phase: protect ≤4, then MMR remainder |
| Dynamic final children 10–18 cross-domain | `DEFAULT_RETRIEVAL_K=12` flat | Dynamic budget by query class + quality stop |
| EvidenceItem lane ranks / domain / obligations | Minimal identity view | Extend contract (additive fields) |
| QueryIR required_domains / bridge_obligations / budgets | Obligations exist; domains/bridges missing | Extend QueryIR without second planner |
| Graph block when no qualified facts | Graph runs with facts_used=0 | **Defect** — return `qualified_graph_evidence_unavailable` |
| Context packet sections | Partial diagnostics | Structured packet assembly |
| Stage timers (directive list) | Partial `timings_s` | Add missing stage keys |
| Alias ranking | shadow only; ranking=false | Stay false until Phase 11 canary |
| Allowlist | `isolated_alias_fixture` name | Add q9 corpus id for canary when authorized |

---

## Graph current state (carried forward)

```yaml
qualified_facts_available: false   # relex_local facts=[] by contract; Neo4j Fact=0 Entity=0
required_behavior: explicit_block_not_hybrid_under_graph_label
```

---

## Implementation order (binding)

Phases 1→12 per directive. Prefer extending:

1. `config.py` budgets/weights  
2. `models/query_ir.py` + `models/evidence_item.py`  
3. `retrieve_planned` wave-1 concurrency + fusion/curation seams  
4. Graph precondition block  
5. Isolated canary on q9 / fixture only  
6. Closeout → HARD PAUSE  

---

## Deliberate non-goals

- No second retrieval orchestrator  
- No Qdrant topology redesign  
- No production ranking / schema backfill / legacy collection deletion  
- No weakening Graph authority gates to invent facts  

# Cross-Domain Retrieval — CLOSEOUT (2026-08-04)

**Directive:** `polymath_cross_domain_retrieval_directive` v1.0  
**Status:** COMPLETE for isolated canary — **HARD PAUSE**  
**Production ranking / schema backfill / Graph without facts / topology migration:** NOT authorized

---

## Final invariant (held)

Original evidence-vector lane always runs. Vocabulary/schema lane resolves corpus-native language (supporting only). Weighted RRF fuses evidence lanes. Up to four strongest children are protected; MMR curates the remainder under a quality-gated dynamic target. Summaries route; vocabulary is not citation authority; child chunks remain answer authority. Graph explicitly blocks when qualified Fact authority is absent.

---

## What shipped (extend-only)

| Seam | Change |
|---|---|
| `models/query_ir.py` | `required_domains`, `bridge_obligations`, budgets, selected corpora |
| `models/evidence_item.py` | Lane/domain/support spine |
| `config.py` | `CROSS_DOMAIN_*` RRF/MMR/protect/budget/Graph-block/curation allowlist |
| `cross_domain_rrf.py` | Configurable weights; direct lane strongest |
| `planned_fusion.fuse_planned_pools` | Uses configured weighted RRF |
| `protected_anchors.py` | Protect 1–4 then remainder for MMR sizing |
| `context_packet.py` | Structured packet in diagnostics |
| `graph_authority.py` | Explicit Graph block |
| `retrieve_planned` | Graph block; allowlist curation; context packet; stage timer aliases |

Defaults after closeout (production-safe):

```yaml
CROSS_DOMAIN_CURATION_ENABLED: false          # canary-only when true
CROSS_DOMAIN_GRAPH_REQUIRE_QUALIFIED_FACTS: true
ALIAS_RETRIEVAL_ENABLED_GLOBALLY: false
ALIAS_RETRIEVAL_RANKING_ENABLED: false        # Level 1 left disabled — no clear canary gain
ALIAS_RETRIEVAL_PRODUCTION_SCHEMA_WRITES: false
ALIAS_RETRIEVAL_PRODUCTION_BACKFILL: false
```

---

## MEASURED canary (q9 corpus `6a766597-…`)

Artifacts under `data_eval/cross_domain/`.

| Gate | Result |
|---|---|
| Schema vectors 1247/1247 @1024 | pass |
| Non-graph fixtures nonempty | **5/5** |
| Schema citations | **0** |
| Graph unqualified → blocked | **pass** (`qualified_graph_evidence_unavailable`) |
| Graph silently returns Hybrid | **false** |
| Protected anchors ≤4 | pass (canary curation enabled) |
| Weighted RRF / direct strongest | pass (config + fusion diagnostics) |
| Alias Level 1 recall gain | **false** → leave disabled |
| Production ranking changed | **false** |
| Production schema mutated | **false** |

Fixture final child counts (quality-gated, not padded): 7, 6, 4, 3, 0(blocked), 3.

---

## Acceptance matrix (summary)

See `data_eval/cross_domain/acceptance_matrix.json`.

```yaml
Fast_Hybrid_retrieval: passed_on_canary
Graph_qualified_fact_use: explicitly_blocked  # no qualified Facts on q9/relex
chat_sse_html: not_in_this_directive_scope
alias_level_1_canary: disabled_for_no_benefit
production_migration_authorized: false
global_alias_activation: false
schema_backfill: false
legacy_collection_deletion: false
```

---

## HARD PAUSE — owner approval required for

1. Production ranking activation  
2. Production schema-vector backfill  
3. Corpus-wide retrieval migration  
4. Enabling `CROSS_DOMAIN_CURATION_ENABLED` outside the allowlist  
5. Enabling Graph without qualified Fact authority  

**Durable bake:** backend image rebuilt (`docker compose … apple-mlx … --build backend`); post-bake Graph block probe PASS; `verify_backend_runtime.sh` OK (embed dim=1024); defaults `CURATION=false` `RANKING=false`.

**STOP.** No further architecture redesign requested.

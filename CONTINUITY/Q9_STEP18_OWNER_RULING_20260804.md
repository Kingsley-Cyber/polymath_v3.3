# q9 Step 18 — Owner ruling (2026-08-04)

**From:** Kingsley Ezeokonkwo  
**Status:** BINDING

## Ruling summary

```yaml
q9_report: accepted_with_documented_caveats
q9_ingestion_and_core_retrieval: passed
alias_shadow_level_0: passed
alias_level_1_canary: authorized   # isolated fixture/canary allowlist only
graph_qualified_fact_use: pending_reprobe
full_chat_html_synthesis: pending
production_migration: not_authorized
```

## Accepted with caveats (not claimed as passed)

- Graph factual usefulness — open until enrichment-terminal re-probe
- Full SSE `/api/chat` HTML synthesis — open until real product-path probe

## Authorized next work

1. Graph re-probe after enrichment terminal (capture `graph_reprobe` fields)
2. Real SSE HTML two-turn probe
3. Empty-probe root cause (battery → 33/33 or explicit unsupported)
4. Cold latency optimization (Relex idle, warmups) without quality change
5. Alias Level 1 canary on allowlist **after** functional defects above
6. Final closeout artifacts under `data_eval/q9_final/` + `CONTINUITY/Q9_FINAL_FUNCTIONAL_CLOSEOUT_20260804.md`
7. Then **STOP** — no architecture redesign; production stays blocked

## Alias Level 1 controls (authorized)

```yaml
alias_level_1:
  enabled_globally: false
  ranking_enabled_for_allowlist: true
  rollback_switch: required
  schema_records_as_citations: 0
```

## Production remains blocked because

- Graph facts usefulness not demonstrated
- Real SSE HTML unproven
- One empty hydrated probe
- Cold Fast/Graph latency not accepted
- Alias Level 1 canary not yet evaluated

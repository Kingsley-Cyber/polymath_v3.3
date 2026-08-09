# Complex Query / Multi-Hop — Implementation Ledger (adopted 2026-08-04/05)

**Status:** Fixture implementation + validation/durability delta COMPLETE · **HARD STOP**  
**Closeout:** `CONTINUITY/COMPLEX_QUERY_MULTIHOP_E2E_CLOSEOUT_20260805.md`  
**Baseline:** `CONTINUITY/COMPLEX_QUERY_SUBQUERY_BASELINE_20260804.md`  
**Production activation:** prohibited until owner GO after review

## Binding rules

1. Extend `QueryPlanV2` / `QueryIR` / `retrieve_planned` — no parallel planner.
2. Subqueries share root embedding, vocabulary results, entity IDs, summary routes.
3. One final rerank per root query; batch Neo4j; batch hydration.
4. Graph Lite / Standard / Deep are internal compile levels under one Graph mode.
5. Isolated fixture: `gsem-e2e-20260804a` via `COMPLEX_QUERY_CORPUS_ALLOWLIST`.
6. Fixture runtime override (`COMPLEX_QUERY_FIXTURE_RUNTIME_ENABLED`) is independent
   of `COMPLEX_QUERY_SUBQUERY_PLANNER_ENABLED` (global stays false).

## Phase checklist

| Phase | Item | Status |
|---|---|---|
| 0–11 | Contracts → Wave-1 → traversal → fusion → fixture E2E | ✅ |
| 12a | same_process_replay | ✅ passed (function-level) |
| 12b | force_recreate_restart_replay | ✅ passed (hashes identical) |
| 13 | Closeout STOP | ✅ HARD STOP |
| Δ | Durable bake + real `/api/chat` SSE + 8-class suite + perf | ✅ |

## Provider-backed synthesis

PASSED 2026-08-05 — LongCat pool override; 3/3 generation probes; see closeout.

## Next execute

**NONE — HARD STOP.** Owner may authorize bounded dark canary hard-stop lift.
Global planner / production activation still prohibited.

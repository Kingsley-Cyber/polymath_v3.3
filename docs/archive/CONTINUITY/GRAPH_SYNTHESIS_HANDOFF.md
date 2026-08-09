# Graph "Mission Control" Synthesis — Diagnosis & Fix Handoff

**Feature:** Brain → Graph Query → 4-lens graph synthesis (`Research` / `Nuance` / `Ideation` / `Gap`).
**Endpoint:** `POST /api/graph/discover` → `services.graph.orchestrator.discover()`.
**Status:** Runs (HTTP 200, no crash) and the **LLM synthesis prose is good**. The **structural signals** behind the lenses (analogies / weak-links / seed-connected bridges) are empty or query-irrelevant on essentially every query. This doc is the root cause + a tiered fix. **Read-only analysis — no code was changed.**

---

## TL;DR

- The four lenses are the *same* bounded query-graph packet re-prompted with a different synthesis goal (`discover.ts`: *"the same shape; the packet caps and system prompt differ per mode"*). Intent: **what's there (Research) → where it conflicts (Nuance) → what to build (Ideation) → what's missing (Gap)**.
- **Generation works.** `auto_synthesis.markdown` returns ~5.3k chars of grounded, cited prose, and the frontend renders it (`AtomicView.tsx:772`, `GraphViewer.tsx:1088` → `auto.markdown || interpretation`). The `interpretation` field is a deterministic stats header **by design** — not where the synthesis lives.
- **The structural scaffolding is broken** via three contained defects (drop → starve → sparse-source). This is what makes Ideation/Gap look empty and bridges look query-irrelevant. **Not a redesign.**

---

## What works (verified live, corpus `authentic_library` f8a0aa85)

| Path | Result |
|---|---|
| `POST /api/graph/query` (build query graph) | **200** — 50 nodes, 321 links, 10 local bridges, 101 local gaps, seeds resolved. Fully query-local & correct. |
| `POST /api/graph/discover` all 4 `synthesis_mode`s | **200**, 20–61s (real LLM call). No 500. |
| `auto_synthesis.markdown` | **Real synthesis** (~5.3k chars, grounded, `[2]`-style citations). Rendered by the UI. |

**Note on the historical blocker:** `services/graph/_orchestrator_legacy.cpython-311.pyc` is **permanently missing** (not on disk, not in git). `discover()` loads it only `if present` and guards every use with `if _legacy is not None`, so it **degrades gracefully** and always runs the tracked fallback `_bounded_discover_without_legacy()` (orchestrator.py:7004–7020). The old "discover 500s / Mission Control down" memory is **resolved**. The `.pyc` does **not** need to be reconstructed for this fix — every primitive needed is tracked Python.

---

## Root cause — three defects in `_bounded_discover_without_legacy()`

`services/graph/orchestrator.py`, `_bounded_discover_without_legacy()` (def ~line 148). It already resolves seeds and computes the subgraph **query-locally** (same functions as `/api/graph/query`):

```
extract_query_entities → expand_subgraph → find_bridges → find_gaps → find_hubs   (lines ~202–247)
```

…but then drops/starves the structural outputs:

### D1 — dropped signals (hard-coded empties)
`orchestrator.py:520/522/523` — the result is assembled with literal empties:
```python
analogies=[],          # line 520
weak_links=[],         # line 522
transfers=[],          # line 523
```
…even though `find_gaps` **already returns** query-local `gap_type ∈ {"analogy","transfer","terminological","missing_edge"}` rows (`services/graph/graph_query.py:903–982`). Those flow into `gaps_v2` (orchestrator.py:550) but are **never surfaced** into the `analogies`/`transfers`/`weak_links` cards.
→ **Effect:** Ideation/Gap cards render empty.

### D2 — starved LLM packet
`_build_insight_packet()` reads structural material from `result._packet_metrics` / `result.metrics` (orchestrator.py:4288–4299, then 4315 / 4348 / 4419 read `structural_analogies` / `transfer_candidates` / `fragile_bridges`). In the bounded path, **`_packet_metrics` is never set** (it's only set in the dead legacy branch at orchestrator.py:7131), and `result.metrics` is a counts dict (lines 533–541) with none of those keys.
→ **Effect:** the LLM packet's analogy/transfer/fragile sections are empty → weaker Ideation/Gap prose (Research/Nuance still read the subgraph text fine).

### D3 — the source signals are sparse & global (the real "all queries" problem)
The elite analogy/terminological/transfer/fragile signals come from `CorpusMetrics` **global** lists, then seed-filtered. Live cache (`graph_metrics_cache`, corpus f8a0aa85):

```
fragile_bridges:      20      structural_analogies: 10
terminological_gaps:   0      transfer_candidates:   0
node_count: 25000 (top RELATES_TO hubs)   cache_mode: compact
→ 0 of 20 bridges and 0 of 10 analogies involve any query seed
```

`find_bridges`/`find_gaps` only emit elite types `if metrics is not None` (graph_query.py:925) and then filter the global lists to the seed-set — which is ~0 for a specific query. So **even after D1+D2 the cards stay empty on most queries.** Sample cached bridge: `"Andrew" → "Density Design"` (random global hubs), not query entities.

### Secondary polish
- **`frontier` = global hubs:** built from positional `nodes[:12]` after an order-losing cross-corpus merge (orchestrator.py:412–421) → high-mention neighbors (`creativity` w/ 784 mentions, `players`) swamp the seeds. `/api/graph/query` avoids this by tagging `is_seed` on nodes (routers/graph.py:282–297).
- **`bridges` with `connected_seed_count: 0`:** these come from `find_bridges`' betweenness fallback sub-path (graph_query.py:701); the seed-anchored path emits `connected_seed_count: 1` (graph_query.py:663). The 0-count ones are global-betweenness nodes, not seed bridges.

---

## Reproduction

```bash
# mint a token in-container, then:
curl -s -X POST http://localhost:8000/api/graph/discover \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"corpus_ids":["f8a0aa85-6cb4-4f64-a973-f9183f1546bb"],
       "query":"natural language processing and data augmentation",
       "synthesis_mode":"research","mode":"auto"}' | jq '{
         interp: .interpretation,
         synth_len: (.auto_synthesis.markdown|length),
         analogies: (.analogies|length),
         weak_links: (.weak_links|length),
         bridges_seed0: ([.bridges[]|select(.connected_seed_count==0)]|length)
       }'
# Observed: synth_len ~5266 (GOOD), analogies 0, weak_links 0, bridges_seed0 = all
```
(`mode` must be one of `auto|connect|gaps|themes`; `synthesis_mode` one of `research|ideation|nuance|gap`.)

---

## Fix plan (tiered)

All edits are in `services/graph/orchestrator.py` (`_bounded_discover_without_legacy`) unless noted. **No new dependency, no model download, no `_orchestrator_legacy` reconstruction.**

### Tier 1 — wiring (cheap; surfaces what's already computed)
1. Derive the dropped fields from `gaps` instead of `[]` (replace lines 520/522/523):
   - `analogies` ← `[g for g in gaps if g.get("gap_type") == "analogy"]`
   - `transfers` ← `[g for g in gaps if g.get("gap_type") == "transfer"]`
   - `weak_links` ← terminological gaps and/or `find_bridges` seed-anchored fragile bridges (whichever the `DiscoverWeakLinkItem` card in `frontend/src/types/discover.ts` expects).
2. Thread warm metrics into the packet: set `result._packet_metrics = metrics` (single-corpus) or a merged `SimpleNamespace` (multi-corpus, mirror `routers/graph.py:452–471`) so `_build_insight_packet`'s `_metric_items(...)` fires (orchestrator.py:4315/4348/4419).
3. Rank `frontier` by `is_seed` then degree/query-relevance instead of positional `nodes[:12]`; drop or relabel `connected_seed_count==0` bridges out of the primary `bridges` card.

### Tier 2 — local signal density (the "works on all queries" fix)
Tier 1 still yields ~0 on most queries because the global precompute (10/0/0) seed-filters to nothing. **Compute the structural signals locally on the seed subgraph** (the `nodes`/`links` `expand_subgraph` already returns), independent of `CorpusMetrics`:
- **analogies** ← shared-neighbor (Jaccard) similarity between entities in the seed neighborhood;
- **weak-links** ← low-confidence / single-path edges within the local subgraph;
- **terminological gaps** ← co-mentioned-but-unconnected seed pairs.
Local-by-construction → non-empty AND query-relevant for any query. Keep the global-metric path as an additive bonus when warm.

### Tier 3 — warm guarantee
Ensure `CorpusMetrics` is warm for PageRank/betweenness (frontier/hub ranking + the global fallback). Reuse the existing self-heal `services/graph/cache_warmup.ensure_graph_metrics_fresh` (signature-keyed, durable claim, non-blocking) on a cold/missing cache.

---

## Acceptance criteria (test-first — house rule: prove with an asserting test, GREEN, before commit)
Live in-container e2e on a real query, asserting:
1. `analogies`, `weak_links`, `transfers` go **0 → N** (Tier 1 on warm corpora; Tier 2 on any corpus).
2. Primary `bridges` are seed-connected (`connected_seed_count ≥ 1`); `frontier` entries are seeds or their direct neighbors (not corpus-wide top-mention hubs).
3. The LLM packet carries non-empty structural sections (Ideation/Gap prose references built ideas / absences).
4. **No regression:** `auto_synthesis.markdown` stays non-empty and grounded on all 4 modes; Research/Nuance unchanged.

Pattern for the e2e: `docker cp test.py polymath_v33-backend-1:/app/_t.py && docker exec -w /app polymath_v33-backend-1 python _t.py` (the image does not bake `tests/`).

---

## Key file:line index
- `services/graph/orchestrator.py`: `_bounded_discover_without_legacy` (~148); local primitive calls (202–247); **hard-coded empties (520/522/523)**; `interpretation` stats header (432–436); result assembly (512–571); packet metric reads (4288–4299, 4315/4348/4419); `_packet_metrics` set only on dead path (7131); LLM call → `auto_synthesis` (614–633, 6294+); legacy dispatch/guards (35–62, 7004–7020).
- `services/graph/graph_query.py`: reusable primitives — `extract_query_entities` (133), `expand_subgraph` (347), `find_bridges` (596; seed-anchored 663 / betweenness 701), `find_hubs` (780), `find_gaps` (885; `gap_type` emission 903–982, `metrics is not None` gate 925).
- `routers/graph.py`: `/discover` handler (1200) + response mapping (1314–1346); `/query` reference impl (200, `_run_one` 262–355).
- `services/graph/analytics.py`: `CorpusMetrics` shape (1717+); global detectors `detect_fragile_bridges` (3059), `detect_analogies_and_terminological_gaps` (3103).
- Frontend: `frontend/src/types/discover.ts` (`GraphSynthesisMode` 415, response model 493–517); `AtomicView.tsx:772`, `GraphViewer.tsx:1088` (renders `auto_synthesis.markdown`).

---

*Prepared from a live, read-only investigation (endpoints exercised, metrics cache + seed overlap probed, code traced). Nothing was modified.*

# Polymath v3.3 — RAPTOR RAG: Heavy Critique, Grounded Roadmap, and GLiNER Plan Review

**Date:** 2026-07-12 (updated same evening with §3.6, Parts 4–9)
**Method:** Full code trace of the ingestion → tree build → indexing → QueryPlanV2 retrieval path, a live audit of the running stack (MongoDB, Qdrant, Neo4j, both MLX sidecars), and 11 live retrieval probes executed inside the backend container through the real `retrieve_planned` path. Every claim below cites the file (and where useful the line) it comes from, or the probe that produced it.

- **Part 1** — The critique: what is actually implemented, live corpus state, test results, findings F1–F10.
- **Part 2** — The roadmap: concrete, code-grounded ways to fix and improve query mapping, speed, and cross-domain / cross-corpus behavior ("the librarian").
- **Part 3** — Review of the planned GLiNER-ReLEx / Concept Card vocabulary bridge, including the full spaCy + GLiNER-ReLEx + Python extraction stack (§3.6).
- **Part 4** — Follow-up deep dives: the quick-upload path, a gap analysis of concept vector search (with a cheaper multi-representation design), and the unified extraction schema that bridges the deterministic stack with the LLM pathway.
- **Part 5** — Closing verdict: how close the fully-implemented roadmap gets to a true cross-domain polymath librarian, and the two frontiers that remain.
- **Part 6** — Live catalog hygiene: stuck `authentic_library` delete, orphan stores, facts/evidence usage, and schema/index gaps for curation vs retrieval.
- **Part 7** — Librarian shelves (analysis only): Direct / Foundational / Adjacent / Bridge / Counterbalance as role-reserved curation — how it maps onto Tier-0, QueryPlanV2, and `ranking_policy`, and why it must **not** reuse `doc_artifact`.
- **Part 8** — Universal indexing contract: grounding an external “versioned universal indexing” recommendation against the real repo — what already exists, what’s mis-targeted (`normalizer.py` vs `CONCEPT_ALIASES`), and how it relates to Part 7.
- **Part 9** — Deterministic-first librarian replan, grounded in the three product tiers (Fast / Hybrid / Graph Augmentation): store contracts, what each tier may use for shelves, phased build order, and tier-aware eval.

---

# Part 1 — The Critique

**TLDR: This is a well-engineered hierarchical *routing* RAG that is not actually RAPTOR, sitting on top of a corpus whose summary layer is ~60–75% hollow.** Retrieval quality in live tests was genuinely good — it found needle answers, blended cross-corpus evidence, and degraded gracefully — but the "tree" is structurally degenerate (98% of sections are byte-identical copies of their single rollup child), the summaries the hierarchy depends on are mostly raw-text placeholders, and the live databases carry significant orphan/drift debt.

## 1.1 What's actually implemented vs. the RAPTOR paper

The paper's pipeline is: embed chunks → **cluster in embedding space (UMAP + GMM soft clustering)** → LLM-summarize each cluster → re-embed → recurse to a root; at query time, search **all nodes across all levels** (collapsed tree), and retrieved summaries **are the evidence**.

What `backend/services/ingestion/summary_tree.py` builds instead is a fixed, document-order hierarchy:

```
child chunks (128 tok target) → parent chunks (1200 tok, 200 overlap)
  → "rollups" (windows of 12–20 consecutive parents)
  → "sections" (grouped by top-level heading, consecutive only)
  → one document profile card
```

Constants: `ROLLUP_WINDOW_MIN = 12`, `ROLLUP_WINDOW_MAX = 20`, `PROFILE_MAX_SECTIONS = 8`, `PROFILE_MAX_CONCEPTS = 12`, `TREE_SCHEMA_VERSION = "polymath.summary_tree.v1"` (`summary_tree.py:25-29`).

There is **no clustering anywhere** — grouping is `group_by_section()` (consecutive parents sharing `heading_path[0]`) plus deterministic windowing. And at query time, summaries are explicitly barred from being evidence (`summary_tree_navigator.py:5-8`): *"Selected nodes are always descended to L1 parent IDs; tree summaries guide discovery and are never returned as final citation evidence."*

The descent is: Tier-0 document cards (`polymath_doc_summaries`, 858 cards) → per-document section search (top 10) → rollups gated by selected sections' `child_node_ids` (top 8) → union of `parent_ids` (≤48/doc) → parent hydration. This is document-gated small-to-big retrieval — closer to a doc-summary index + auto-merging than to RAPTOR. The trade is deliberate (bounded cost, deterministic, resumable) and the code is honest about it, but two of RAPTOR's core value propositions are gone:

1. **Cross-structure thematic nodes.** Content about one theme scattered across chapters never lands in a shared summary node, because grouping follows document order, not meaning. The only cross-chapter abstraction is one 3–4 sentence document profile.
2. **Abstraction-level matching.** Abstract questions can't retrieve abstract nodes as context. Every answer is assembled from leaf parents; the multi-lane query decomposition (`query_plan.py`, phrase-heuristic, no LLM) has to compensate.

## 1.2 Live corpus state (audited on the running stack)

12 corpora in Mongo, but only 5 have vectors. The big three healthy ones: `polymath_v2` (498 docs, 278k hrag points, 42,990 tree nodes), `ecommerce_AI_FILM_SCHOOL` (79 docs, 65k points, 1,867 tree nodes), `markbuildsbrands` (103 docs, 4.3k points, 504 tree nodes). Mongo: 868k child chunks, 280k parents, 45.5k tree nodes. Neo4j: 951k Facts, 792k Entities, 624k Chunks.

Serious hygiene problems:

- **`authentic_library`: 486 docs, 561k Mongo chunks, 136k parents — and zero Qdrant vectors, zero tree nodes.** `corpus_readiness` says `needs_repair` with 1,715 pending summary jobs and blocked pipeline jobs. Half the ingestion (Mongo side) ran; the retrieval side never materialized. It is silently unqueryable (probe T7 returned nothing).
- **Orphans everywhere.** `corpus_a42992d0_hrag` holds 153k points with no corpus record and no naive/graph siblings; `corpus_7c8ec461_hrag` similar; the `corpus_16a5694b_*` group exists empty with no corpus; 7 corpus_ids in `summary_tree` and 19 in `polymath_doc_summaries` belong to deleted corpora. Corpus deletion clearly doesn't cascade.
- **Three sources of truth disagree** for `polymath_v2`: naive 307,770 vs hrag 278,284 vs Mongo `chunks` 240,876. Vectors exist for chunks that no longer exist in Mongo.

## 1.3 Test results (11 live probes through the deployed QueryPlanV2 path)

| # | Probe | Scope | Outcome |
|---|-------|-------|---------|
| 1 | "Difference between a zoom and a dolly shot" | film corpus | **Cold: correct chunk ranked #10 of 14** (reranker timeout after 20s, degraded). **Warm re-run: #1 at 0.958.** |
| 2 | "Four components of nonviolent communication" | polymath_v2 | Correct NVC chunks #1/#2 (0.89/0.87). `vocabulary_resolution` took 18.1s of 31.6s total. |
| 3 | "How do quiz funnels work…" | markbuildsbrands | Correct transcript found; a vocabulary lane timed out (degraded) but results fine. |
| 4 | "FACS → character animation" | film corpus (cross-domain) | FACS manual #1/#2, animation-agent doc bridged correctly. Reranker timeout again (cold). |
| 5 | "CBT techniques for directing actors" | 2 corpora | **Good cross-corpus blend: 4 psych + 5 film chunks**, all topical; corpus floor satisfied both. |
| 6 | "Boiling point of tungsten" (negative control) | film corpus | **`answerable: true`, coverage 1.0** — served tungsten *lighting* chunks. Lane-coverage ≠ answerability. |
| 7 | Query against `authentic_library` (no vectors) | — | 0 results, `coverage 0`, correct refusal — but failure labeled `embedding TimeoutError`, which is misleading. |
| 8 | Consumer-culture query against UGO (no tree) | — | Flat fallback worked; 2 relevant chunks. |
| 9 | "Marshall Rosenberg on feelings and needs" | 4 corpora | **Routing precision excellent** (NVC book top route, 0.98 lane score). But corpus floor **seated 3 irrelevant chunks at scores 0.06–0.09** in the final packet, and the #1 evidence chunk was **a 300-char markdown table-separator (all dashes)**. |
| 10 | Same, rerank disabled | polymath_v2 | Dense+sparse fusion alone still ranked the right doc #1/#2 — the base index is sound. |
| 11 | Zoom/dolly at `qdrant_mongo_graph` tier | film corpus | Graph tier worked (14 facts fused), results *improved* (Bruce Block's zoom passage surfaced #2). |

Latency: 12–40s per retrieval (embed 0.6–4s, vocabulary 1.8–18s, tree routing 1.6–7s, rerank 5–20s). Every "degraded" status in every test traced to one cause: **single-slot local MLX sidecars** (embedder :8082, reranker :8081, both `mps`, both serialized). Cold reranker = guaranteed first-query timeout; concurrent queries = embedding timeouts.

## 1.4 Findings, ordered by severity

**F1 — The summary layer is mostly fake.** Sampling 500 `chunk_type=summary` points per corpus: **61% (film corpus) and 75% (polymath_v2) have `summary_text` byte-identical to `chunk_text`**, with empty `summary_model` and `summary_type: parent_retrieval_replacement` (the placeholder default at `qdrant_writer.py:862`). These are placeholders where raw parent text was re-indexed as its own "summary." The hierarchical-abstraction lane (`funnel_a.py`, `top_k_summary=24–48`) is therefore mostly a duplicate child search with longer text. 500 summary jobs sit queued; the healed ones (`summary_model: summary_tree_heal`) show what the layer *should* be — the full structured contract lives in `services/ingestion/summary_semantics.py` (summary, key_points with child evidence anchors, semantic_chunk_type, key_terms, mechanisms, abstraction_level).

**F2 — The tree is degenerate: 98% of sections are their own child.** Of 2,000 sampled sections, **1,964 have exactly one rollup child with an identical summary string** (code path: `if len(rollups) == 1: section.summary = rollups[0].summary`). Corpus-wide ratios confirm it: 21,432 sections vs 23,394 rollups (~1.09 rollups/section). Both levels get separately embedded, stored in Qdrant, and searched in a two-stage gated descent (sections top-10 → rollups top-8) — the second stage re-scores near-identical vectors. Pure duplicated storage plus 1.6–7s of query latency for near-zero routing information. Root cause: PDF→markdown docs surface "Page N"-style top-level headings, so `group_by_section`'s consecutive-top-heading grouping produces thousands of tiny sections each holding one rollup window.

**F3 — Not RAPTOR where RAPTOR matters** (see §1.1). No embedding-space clustering, no recursion, summaries never usable as evidence. The payload writer even labels nodes "pre-embedded RAPTOR section or rollup node" (`qdrant_writer.py:1157`) — the name oversells the mechanism. As routing infrastructure it works; as RAPTOR it's a D, and the thematic questions the paper targets are handled instead by lane decomposition + the Neo4j graph tier.

**F4 — Storage triplication.** For modern corpora the same child vector is written to `_hrag`, `_naive`, *and* `_graph` collections, and the same summary vector to both `_hrag` and `_naive` (fd460347: hrag 65,149 = naive 65,149 = 55,774 children + 9,375 summaries; graph = the same 55,774 children again). Three full dense copies per child × 1024 dims. Funnel A only reads hrag summaries; funnel B only reads naive+graph children — the split buys filter isolation that the existing `chunk_type` payload filter (`funnel_a.py:112-117`) already provides.

**F5 — Fairness quota pollutes precision queries.** The corpus floor (`ranking_policy.py:1678-1816`) guarantees every requested corpus a final-packet seat. On T9 that meant three 0.06–0.09-score chunks (sound design, consumer behavior, a YouTube transcript) shipped alongside 0.98 NVC evidence. The code *does* run an eligibility check (`_passes_relevance_floor` at `ranking_policy.py:1700`, floors 0.35 BROAD / 0.80–0.85 SPECIFIC as ratio-to-top), yet the observed packet still seated near-zero chunks — the check evaluates against normalized/relative scales (`relevance_by_idx`, min-max MMR values) that don't match the calibrated scores shown in the packet, and the reserve path adds +0.10 (`ranking_policy.py:1775`). Intent and behavior disagree; the scale needs to be pinned.

**F6 — Answerability is lane coverage, not answer presence.** The tungsten probe (T6) returned `answerable: true, coverage: 1.0` because each concept lane ("tungsten", "boiling", …) matched *something*. The downstream LLM is the only hallucination gate. The `sufficiency.answerable` flag name promises more than the mechanism delivers.

**F7 — Routing-card pollution.** Tier-0 `polymath_doc_summaries` (`SHARED_DOCSUM`, `tier0.py:23`) retains cards for deleted corpora; the first probe ranked test-junk docs ("tier0 probe note", "verifier fix probe2") from a ghost corpus as top-3 routes. On unscoped/MCP default-to-all queries, dead corpora still steal routing slots.

**F8 — Chunk hygiene gaps.** The #1 reranked evidence chunk on T9 was a run of dashes (markdown table separator) under a real NVC heading. `NOISY_KINDS` filters navigation/bibliography kinds (`funnel_a.py:144-154`), but nothing drops low-alpha/separator content, and the cross-encoder loves it. Also observed: OCR-garbage heading paths (`"LJ * imme ot"`), empty `doc_name`/`filename` on summary points, and facet tags bleeding across domains (`agency_preservation`, `emotional_patterns` stamped on cinematography, stage-combat, and DaVinci Resolve chunks alike).

**F9 — Operational fragility is the #1 practical quality problem.** Warm, serial queries are excellent; cold or concurrent ones degrade. The 20s reranker timeout (`RERANKER_TIMEOUT_SECONDS`, `config.py:1297`) burned the first query in two tests; parallel probes produced embedding timeouts. There is no queue — ingest, chat, and graph promotion all contend for one MPS slot each. 12–40s per retrieval is rough for interactive chat, and 18s `vocabulary_resolution` (T2) against 360k `corpus_lexicon` rows deserves a profile pass.

**F10 — Embedding hygiene.** `embed_queries` (`services/embedder.py:611`) sends raw text; the sidecar (`embedder/main.py`) does a plain `model.encode`. Qwen3-Embedding is instruction-tuned for asymmetric retrieval — queries should carry the instruction prefix, worth a few points of recall per the model card. Tree nodes embed `section_range + summary` truncated to 3,000 chars, and doc/tree/summary/child vectors all share one undifferentiated space.

## 1.5 What's genuinely good (credit where due)

- **The retrieval stack above the storage is strong**: hybrid dense + server-side BM25 sparse (with a documented fix removing pool-max score fabrication), per-corpus Mongo `$text` fallback for legacy layouts, cross-encoder rerank with calibration, multi-lane decomposition with translation/step-back lanes, coverage repair, grounding filter, and a deterministic retrieval cache keyed on a corpus artifact epoch (`retriever/__init__.py:763`, `:3350` — correct invalidation).
- **Cross-corpus is real, not bolted on**: per-corpus collections queried in parallel, fair-mode per-corpus budgets in funnel A (`funnel_a.py:61-86`), corpus-floor representation, a 32-corpus cap, corpus_id filters as defense-in-depth inside every funnel, and prefix-collision ownership guards. T5 and T9 prove routing + blending works.
- **Everything degrades instead of failing**: every component failure induced (cold reranker, missing vectors, missing tree, missing collections) produced a labeled degradation with whatever evidence was still reachable.
- **The tree build is disciplined engineering**: pure/deterministic structure, injected LLM with extractive fallback, stable node IDs, resumable upserts, bounded profile inputs, two-tier query-time fallback (pre-embedded Qdrant nodes → Mongo + query-time embedding).
- **Parent/child mechanics are correct**: 1200/128-token parent/child split with 200 overlap, child-match → parent hydration with query-guided excerpting (`hydrate.py`), tables handled as first-class chunks.

## 1.6 Verdict on cross-domain / cross-corpus ability

**Cross-corpus: works, B.** Mechanically sound (T5's 4+5 blend, T9's precise routing), with two caveats: the fairness quota injects noise on single-corpus-answer queries, and there is *no indexed abstraction above the document level* — no cross-document or cross-corpus summary nodes — so cross-corpus synthesis is entirely juxtaposition + LLM + the Neo4j entity/relation layer (which did contribute: 14 facts on T11, and `RELATES_TO` edges natively carry `corpus_ids` lists).

**Cross-domain within a corpus: adequate but bounded.** T4 bridged FACS→animation via lane decomposition. But descent budgets (≤16 routed docs, 5 sections, 4 rollups, 48 parents per doc) truncate breadth queries, and without clustering there's no thematic node to catch concepts that co-occur across many documents.

---

# Part 2 — Grounded Improvement Roadmap

Everything below names the exact files that change and reuses machinery that already exists. Ordered by leverage-per-effort within each theme.

## 2.0 Quick wins (days, not weeks — do these first)

### QW-1. Stop double-indexing placeholder summaries (attacks F1)
Two options, both small:
- **Read side (immediate):** in `funnel_a.py:_search_summaries` (~line 112), add to `must_not_conditions` a match on empty `summary_model`, so placeholder points stop competing in the abstraction lane. One filter clause; zero migration.
- **Write side (correct):** in `qdrant_writer.py` around line 862 (where `summary_type` defaults to `parent_retrieval_replacement`), refuse to upsert a summary point when `summary_text == chunk_text` and `summary_model` is empty — the child point already covers that text. Then drain the ~500 queued summary jobs and let the heal path (`summary_model: summary_tree_heal`) backfill real summaries. `summary_semantics.py` already defines the full structured contract; the pipeline exists, it's just starved.

### QW-2. Collapse the degenerate section→rollup double search (attacks F2, saves 1.5–7s/query)
In `summary_tree.py`, when `group_by_section` yields a section with exactly one rollup, emit **only one node** (keep the rollup, alias the section ID to it). In `summary_tree_navigator.py`, when a selected section's `child_node_ids` has length 1, skip the second-stage rollup search and take the child directly. Also fix the root cause: treat "Page N" headings as non-structural in `group_by_section` (a regex guard) so PDFs stop producing thousands of single-window sections. Halves tree storage; removes a full Qdrant round-trip per document on most documents.

### QW-3. Pre-warm and keep-alive the sidecars (attacks F9's worst symptom)
The reranker sidecar already self-probes at startup (`reranker/main.py:41` `_predict_health_probe`), but nothing on the backend side ever wakes it before the first user query, and there is no keep-alive (grep confirms the only warmups are Modal deploys `modal_deployer.py:394` and graph cache `graph/orchestrator.py:112`). Add to the FastAPI lifespan: fire `GET /health` + a 1-pair `/rerank` and a 1-string embed at startup, then a background task pinging every ~4 minutes. This alone removes the guaranteed-timeout-on-first-query behavior seen in T1 and T4. Optionally add one retry-on-timeout for the rerank call in `retriever/__init__.py` — the fallback already degrades gracefully (`tests/test_planned_retrieval.py:319` proves the path), so a single retry is safe.

### QW-4. Filter separator/low-alpha chunks (attacks F8)
At ingest (`tier_chunker.py`): drop or merge chunks whose alphanumeric ratio is below ~0.3 (the all-dashes table separator fails this trivially). Defense in depth at rerank assembly: skip candidates failing the same test before they reach the cross-encoder. Cheap, deterministic, kills the T9 embarrassment class.

### QW-5. Cascade deletes + one-off orphan sweep (attacks F7, hygiene)
Write one admin script (pattern already exists in `backend/scripts_probe_*.py`) that: deletes `polymath_doc_summaries` points whose `corpus_id` has no corpus record (19 ghost corpora), drops orphan `corpus_*` Qdrant collections (a42992d0, 7c8ec461, 16a5694b), and removes `summary_tree` rows for dead corpora. Then patch the corpus-delete path to do all three inline. Fixes the ghost-corpus routing pollution seen in the very first probe.

### QW-6. Qwen3 instruction prefix on queries (attacks F10)
In `services/embedder.py:embed_queries` (line 611), prepend the Qwen3 instruction template (`Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: {q}`) to query texts only. Documents stay as-is — this is exactly the asymmetric scheme Qwen3-Embedding was trained for, so **no re-embedding of the corpus is required**. Keep the raw text as the cache key (or version the cache key) so the retrieval cache doesn't serve stale mixed vectors. Free recall.

## 2.1 Better query mapping

The pieces of a real query-understanding layer already exist in the repo — they're just disconnected or disabled:

- `build_query_plan_v2` (`query_plan.py:1251`) is deterministic and phrase-heuristic: *"Build a bounded phrase-aware plan without an LLM call."* Good bones, but it can only rearrange the user's own words.
- `vocabulary.py` (`corpus_vocabulary.v3`) already resolves user terms against the corpus lexicon (Qdrant `entity_lexicon` points + Mongo exact/fuzzy aliases + optional Neo4j neighbors), with the right epistemics: *"expansions … remain exploratory unless the user's own words establish the concept directly"* (`vocabulary.py:1-7`).
- `grounded_planner.py` (`grounded_query_planner.v5`) is a fully built, constrained LLM query planner — translation probes, step-back probes, obligations, dependencies — where **every introduced term must cite a lexicon entry id** (`grounded_planner.py:57`). It is dark in production because it requires three gates: `GROUNDED_QUERY_PLANNER_ENABLED` (`config.py:413`), a model, and a durable budget.

Recommendations:

**QM-1. Turn the grounded planner on, selectively.** Don't run it on every query — trigger it when the deterministic plan is weak: (a) `vocabulary.py` resolves zero or low-confidence lexicon matches for a core lane, or (b) the query contains no corpus-native terms (all lanes fall back to raw phrases). Those two signals are already computed during vocabulary resolution. With the Mongo cache (`CACHE_COLLECTION = "grounded_query_plans"`, `grounded_planner.py:26`) the marginal cost after warm-up is near zero for repeated phrasings. This is the single biggest "understands what I meant" upgrade available without writing new code.

**QM-2. Make answerability honest (F6).** `_evaluate_sufficiency` measures lane coverage. Keep it, but rename the emitted flag to `lane_coverage_met`, and derive `answerable` from evidence strength instead: require the top calibrated rerank score to clear an absolute threshold (the calibration machinery in `ranking_policy.py` already produces comparable scores) AND coverage. T6 (tungsten) would then correctly report low confidence, and the chat layer can say "my sources discuss tungsten lighting, not its boiling point" instead of hallucinating cover.

**QM-3. Route by answer shape.** `ranking_policy.py:495-505` already distinguishes `QueryNeed.BROAD` from SPECIFIC (different relevance floors). Extend that intent to retrieval composition: for BROAD/overview queries, allow real (non-placeholder) section/rollup summaries and document profiles to be **citable evidence** — the payloads carry `abstraction_level` from `summary_semantics.py` already — instead of always descending to leaf parents. This restores RAPTOR's abstraction-matching for the query class that wants it, at zero index cost once QW-1 fills real summaries.

**QM-4. Conversation anchoring is already half-built — finish it.** The ranking layer has a `document_anchor_reserve` protected reason (`ranking_policy.py:1719-1723`). Feed it: when a conversation has an accepted answer from doc X, pass X as an anchor into the next `retrieve_planned` call so follow-ups ("what else does he say about that?") don't re-route from scratch. This is what makes a librarian feel like they remember what book is open on the table.

## 2.2 Speed

Measured budget today: embed 0.6–4s, vocabulary 1.8–18s, tree routing 1.6–7s, rerank 5–20s. Targets and levers:

**SP-1. Kill sidecar contention (the dominant cause).** All degradations traced to two single-slot MLX processes. In order of effort:
1. QW-3 warmup/keep-alive (removes cold starts).
2. **Priority lanes:** wrap sidecar calls in an asyncio priority semaphore in `services/embedder.py` / reranker client — retrieval-critical calls preempt ingestion/backfill batches. Ingestion embedding is throughput work; chat embedding is latency work; today they queue FIFO on one MPS slot.
3. Second embedder instance for ingestion (even CPU-only) so bulk work never touches the retrieval slot — the config already supports per-purpose endpoints (`_embedding_config_for_query`, `retriever/__init__.py:282-1000` caches per-corpus embed config).

**SP-2. Batch the plan's embeddings into one call.** `embed_queries` (`embedder.py:611`) already "deterministically deduplicates texts … sends all misses through one batch call." Audit callers: vocabulary resolution and lane retrieval each embed lane variants at different times (observed as multiple sidecar round-trips inside one query, 18.1s worst case in T2). Collect every dense text the plan will need — lanes, vocabulary probes, tree navigation query — and embed once up front, passing vectors down. Structurally: `retrieve_planned` builds the full text set from `QueryPlanV2` before calling `vocabulary.resolve` / funnels.

**SP-3. Cache vocabulary resolution.** The resolver is deterministic given (query, corpus set, lexicon epoch). Store its output in Mongo keyed exactly like the retrieval cache (`_corpus_artifact_epoch`, `retriever/__init__.py:763`) — the epoch pattern is already proven there. Follow-up queries in a conversation usually share vocabulary; today they pay 1.8–18s again.

**SP-4. Collapse the collection triplication (F4).** One collection per corpus with `chunk_type` payload-indexed (indexes already created via `_create_payload_index_with_retry`, `qdrant_writer.py:387`) serves funnel A (`chunk_type=summary`) and funnel B (`chunk_type=child`) with identical filters. That's 3× less write amplification, 3× less RAM for children, and one fewer collection-availability check per corpus per query. Migration: write new corpora single-collection first, backfill old ones with the existing `upsert` machinery which is already resumable.

**SP-5. Rerank less, earlier.** 5–20s to rerank ~40–85 candidates on MPS. Two levers: (a) tighten `retrieval_k` → rerank ~24 after fusion — T10 proved dense+sparse fusion alone already ranks well, so the cross-encoder is polishing, not rescuing; (b) truncate candidate text to the query-relevant excerpt before scoring — `hydrate.py` already computes query-guided excerpts; feed the excerpt, not the full 1200-token parent, to the reranker. Cross-encoder cost is linear in tokens.

**SP-6. Tree descent to one stage.** After QW-2, most documents have a single meaningful tree level; the descent becomes one Qdrant search + parent union. Combined with SP-4, per-document routing drops to one round-trip.

Realistic outcome: warm single-user queries go from 12–40s to roughly 3–8s (embed <1s batched, vocabulary cached ~0s on repeat, routing ~1s, rerank 2–5s), and the cold/concurrent cliff disappears.

## 2.3 Cross-domain, cross-corpus, and the librarian

The goal stated: *"the corpus actually feels like a living knowledge base librarian that knows what is being asked and curates the best books."* The system already has the librarian's index cards (Tier-0) and reading room (hydration); what's missing is shelf-level knowledge, thematic memory, and the ability to answer at the right altitude. **Part 7** expands this into Direct / Foundational / Adjacent / Bridge / Counterbalance role reserves (`librarian_card` + `shelf_reserve`). **Part 8** grounds the related “universal indexing contract” advice against the real ingest/readiness spine. **Part 9** replans the build as deterministic-first and maps every shelf mechanism onto Fast / Hybrid / Graph store contracts so Neo4j never becomes a Fast dependency.

**LB-1. Fix the corpus floor scale mismatch (F5) — precision first.** In `ranking_policy.py:1700-1708`, the eligibility check runs `_passes_relevance_floor` against relative/normalized scores while final packets carry calibrated scores; T9 seated 0.06–0.09 chunks despite a 0.35 floor. Pin the floor to the calibrated rerank score (same scale as the packet), e.g. seat a corpus only if its best candidate ≥ max(0.25, 0.3 × top calibrated score). A librarian who can't find anything relevant on a shelf says so — the diagnostics field `corpus_floor.skipped` already exists to carry that message to the UI.

**LB-2. Corpus routing cards — route shelves before books.** Tier-0 routes documents; nothing routes *corpora*. Add one profile card per corpus to `SHARED_DOCSUM` (`tier0.py:23`) with a distinct `kind`, aggregating its document profiles (domains, top concepts, doc count — the doc profile builder in `summary_tree.py` already extracts `PROFILE_MAX_CONCEPTS=12` concepts per doc; roll them up). Unscoped and MCP default-to-all queries then do: corpus cards → pick 2–3 shelves → doc cards within them. This cuts fan-out (today: parallel search of every readable corpus), speeds up multi-corpus queries, and is the mechanism that makes "which of my libraries even covers this?" answerable — the librarian's first move.

**LB-3. Thematic nodes: the missing RAPTOR layer, done offline and cheaply.** The vectors are already in Qdrant — clustering requires **no re-embedding**, just a scroll. Per corpus, nightly/on-epoch-change: scroll parent vectors → cluster (agglomerative or HDBSCAN; even k-means works at 280k×1024) → LLM-summarize each cluster with the existing `summary_semantics.py` contract → upsert as `chunk_type=theme` points carrying `member_parent_ids` and `corpus_id`. Retrieval: funnel A picks them up with a one-line filter change; BROAD queries (QM-3) can cite them directly; SPECIFIC queries use them as an additional router (theme → member parents). This restores both missing RAPTOR properties — cross-structure thematic grouping and abstraction-level matching — without touching the write path or the deterministic tree. Start with one corpus (`markbuildsbrands`, 4.3k points) to validate cluster quality before scaling.

**LB-4. Cross-corpus themes via the graph you already have.** Neo4j `RELATES_TO` edges natively carry `corpus_ids` lists (verified live), and the graph holds 951k facts. A "bridge card" materialization — entities whose edges span ≥2 corpora, with their top relations — indexed into the shared routing collection gives the librarian the line "psychology shelf and film shelf both discuss *emotional regulation*; here's how they connect." T5/T11 prove the graph tier contributes; this makes its cross-corpus knowledge visible at routing time instead of only at evidence-fusion time.

**LB-5. Say what you did.** Every piece already exists in diagnostics (`plan_diagnostics.tier0` routes, `corpus_floor` meta, lane coverage, vocabulary matches). Surface a one-line retrieval rationale in chat: "Looked in *NVC* (routed 0.98) and *Psychology shelf*; skipped *markbuildsbrands* (nothing above threshold)." Zero retrieval changes — it's presentation of data already computed, and it's most of what makes a librarian feel alive rather than an opaque search box.

**LB-6. Repair or quarantine `authentic_library`.** 486 docs / 561k chunks that silently return nothing (T7) is the opposite of a living library. Either drain the 1,715 pending jobs (after QW-1 makes them meaningful) or have `corpus_readiness` mark it visibly non-queryable in the UI and the MCP tool description. Also fix the misleading `embedding TimeoutError` label for the no-vectors case — the readiness collection already knows the real reason.

## 2.4 Architecture posture (bigger, optional)

- **One retrieval queue.** In-process asyncio with no admission control means chat, MCP, ingest, and graph promotion contend invisibly. A single priority queue in front of sidecar + Qdrant calls (retrieval > interactive ingest > backfill) makes tail latency predictable. Do this before considering any horizontal scaling.
- **Quantize Qdrant vectors.** ~750k+ points × 1024 float32 ≈ 3GB+ resident before overhead, tripled by F4. After SP-4 dedup, enable scalar int8 quantization (Qdrant supports rescoring with originals on disk) — typical recall loss <1% for a ~4× RAM cut.
- **Consider serving rerank on CPU int8 (ONNX)** for the 0.6B reranker: eliminates MPS contention with the embedder entirely, and at ~24 candidates × excerpt-length inputs (SP-5) CPU latency is competitive with a contended MPS slot.
- **Name the architecture honestly.** After LB-3 you legitimately have "hierarchical routing + thematic RAPTOR layer." Until then, internal naming ("pre-embedded RAPTOR node") writes checks the code doesn't cash — rename to `summary_tree_node` and keep "RAPTOR" for the clustered layer.

---

# Part 3 — Critique of the GLiNER-ReLEx / Concept Card Plan

The plan: use the RunPod Flash worker (`gliner-relex-large-v0.5`) to materialize **Concept Cards / Semantic Concept Profiles** — technical + plain definitions, lexical forms, user-language mappings (descriptions / problem phrasings / goal phrasings), graph links, evidence anchors — with **multi-representation embeddings** (`concept_name_vector`, `definition_vector`, `lay_expression_vector`, `example_query_vector`) feeding a "Selected-Corpus Global Vocabulary Search" and the Optional Grounded Planner in QueryPlanV2.

The full extraction stack behind it (clarified after the first draft of this review): **spaCy** as the deterministic linguistic foundation (sentence segmentation, SVO parsing, negation detection, Matcher rules for non-negotiables like "AU12"), **GLiNER-ReLEx** as the semantic engine (joint entity+relation extraction, zero-shot open labels, provenance anchoring), and **Python** as the durable orchestrator (identity, structural/ontology validation, reconciliation-based repair) — living **only in the RunPod extraction path** and feeding Grounded Routing. §§3.1–3.5 review the concept-card/routing half; **§3.6 reviews the extraction stack layer by layer against the code**.

## 3.1 First, the uncomfortable truth: ~70% of this plan is already built

Before writing any new code, take inventory — the plan describes several systems that exist in the repo today under different names:

| Plan element | Existing implementation | Status |
|---|---|---|
| RunPod Flash worker w/ `gliner-relex-large-v0.5` | `runpod_flash_extractor/app.py` (contract `polymath.runpod_gliner_relex.v2`, line 23), backend engine `runpod_flash` in `extraction_contract.py:35`, full settings/router/benchmark wiring | **Built, deployable** |
| Canonical ontology label mapping | `_canonical_label` / label map in `runpod_flash_extractor/app.py:38-43`; the GLiREL fine-tune ships a fixed 30-label set (`incoming_glirel/README_DROP.md:33-38`) | **Built** |
| Source-backed relation evidence (sentence spans) | `_evidence` + `_sentence_spans` in `app.py:186-240`; relations without evidence are dropped (`app.py:327`) | **Built** |
| Concept cards w/ canonical terms, aliases, abbreviations, definitions | `corpus_lexicon.py` — entries carry `canonical_name`, `aliases`, `abbreviations`, `definitions`, `structural_contexts`, `contextual_usages`, `application_contexts`, `related_concepts`, `components` (see `_build_embedding_gloss`, line 1402) | **Built, populated: 360k rows / 248k Qdrant points for polymath_v2** |
| Concept embeddings in Qdrant | `upsert_lexicon_entries` (`qdrant_writer.py:1441`) embeds one `embedding_gloss` per entry into the per-corpus `schemas` collection, with delta re-embed + vector reuse (`corpus_lexicon.py:2782-2947`) | **Built** |
| Selected-corpus global vocabulary search | `vocabulary.py` (`corpus_vocabulary.v3`) — exact payload-indexed alias hits first, dense gloss hits second (`search_lexicon_entries`, `qdrant_writer.py:1562-1587`), Mongo fuzzy fallback, Neo4j neighbor attach | **Built and live** (it's the 1.8–18s stage in the probes) |
| Optional Grounded Planner using vocabulary evidence | `grounded_planner.py` v5 — corpus-native rewrites where every introduced term cites a `lexicon_entry_id` (line 57) | **Built, disabled** (three gates, `config.py:413`) |
| Entity vs. mention distinction | Partial: `normalize_identity` (`corpus_lexicon.py:192`) + trusted alias methods (`vocabulary.py:53-59`) + embedding-similarity merge in lexicon build | **Partial** |
| Plain-language gloss as "semantic landing zone" | `_build_embedding_gloss` renders naturalized labels + "Useful for…" + "Also called…" — a gloss, but derived from *source* text, not *user* language | **Partial — the gap** |
| User-language mapping (descriptions, problem phrasings, goal phrasings) | Nothing | **Not built — the genuinely new part** |
| Multi-representation vectors per concept | One vector per entry today | **Not built** |

So the honest framing of the plan is: **extend `corpus_lexicon` with a user-language layer and multi-vector storage** — not build a new concept system. If it's built as a parallel system, there will be two lexicons with two resolvers and two ingestion paths to keep consistent; the existing one already solved hard problems the plan hasn't hit yet (delta re-embedding with vector reuse, epoch invalidation, owner-verified collections, degraded-embedder exact-match fallback).

## 3.2 The core design flaw: GLiNER-ReLEx cannot produce the most valuable fields

GLiNER/ReLEx is a **span extractor**: it finds and types text that is *present in the source*. The plan's highest-value fields — plain-language gloss, lay descriptions ("moving naturally while talking"), problem phrasings ("the actor looks stiff"), goal phrasings ("make the speaker look natural"), example queries — are **not in the source text**. An expert book does not contain the novice's words for its own concepts; that's the entire premise of the vocabulary gap. No span extractor can emit them.

Those fields are **generative**: they require an LLM writing *as a lay user* about an extracted concept. So the correct pipeline is:

1. **GLiNER-ReLEx (RunPod Flash, already built):** extract mentions, entities, typed relations, evidence spans → densify the lexicon and the Neo4j graph. This is what it's good at, and the deploy/benchmark gating in `runpod_flash_extractor/README.md` (canary → 100 → 500 → 5,000 chunks with yield/budget gates) is the right way to scale it.
2. **LLM enrichment pass (new, small):** for each lexicon entry above a salience threshold, generate the `user_language` block (plain gloss, 2–3 problem phrasings, 2–3 goal phrasings, 2–3 example queries) from the entry's existing source-backed material (`definitions`, `contextual_usages`, `application_contexts`). This mirrors exactly how `summary_semantics.py` runs structured LLM generation with deterministic parsing and caps — reuse that pattern, including the extractive fallback discipline (if the LLM fails, the entry simply has no user-language block; never fabricate structure).

The plan as written assigns step 2's output to step 1's tool. That's the thing to fix on paper before writing code.

## 3.3 What's genuinely right about the plan

- **It attacks the real observed gap.** Live probes succeeded on expert phrasing; the resolver today can only bridge via terms that *appear in the source* (aliases, definitions). A user saying "the actor looks stiff" matches nothing unless a book literally says that. User-language mapping is the correct missing layer, and glosses-as-semantic-landing-zones is the correct mechanism — the codebase already bets on it (`embedding_gloss` exists precisely for this).
- **Multi-representation is directionally correct.** One mega-gloss vector averages away the lay register. A separate user-language vector genuinely will sit closer to novice queries. Qdrant named vectors support this in-place on the existing `schemas` collection points — no new collection needed.
- **Keeping it exploratory is already enforced.** The resolver's rule — expansions *"remain exploratory unless the user's own words establish the concept directly"* (`vocabulary.py:5-6`) — and the planner's citation requirement (`grounded_planner.py:57`) are exactly the guardrails generated (non-source-backed) lay phrasings need. Tag the generated block with provenance (`user_language_model`, `generated: true`) and the existing epistemics carry over.
- **Evidence anchoring is the right instinct** and is already the norm: lexicon entries carry extraction provenance, RunPod relations carry `evidence_phrase` (`app.py:335`). Requiring `chunk_id`/`sentence_id` on concept evidence keeps the cards debuggable.

## 3.4 Risks the plan underestimates

1. **Latency.** Vocabulary resolution is *already* the slowest pre-retrieval stage (1.8–18.1s, T2). Four vectors per concept means four dense searches per lane unless batched. Mitigate: one `embed_queries` batch for all probe texts (SP-2), Qdrant Query API prefetch across named vectors in a single request, and the vocabulary cache (SP-3). If this ships without SP-2/SP-3, the librarian gets smarter and slower.
2. **Vector inflation.** 360k entries × 4 named vectors × 1024 dims ≈ 1.4M extra vectors (~5.6GB float32) for polymath_v2 alone — on a stack already carrying F4's triplication. Mitigate: (a) enrich only salient entries (top ~10–20% by evidence count / graph degree / retrieval hit-rate — hit-rate is observable from the resolver diagnostics); (b) merge `lay_expression_vector` + `example_query_vector` into one `user_language_vector` (they're the same register; the split buys little); (c) drop `concept_name_vector` entirely — exact/fuzzy alias matching on the payload index (`search_lexicon_entries` exact-first design) already handles name matching better than dense retrieval, and the name is inside `embedding_gloss` anyway. Recommended end state: **two representations** (`gloss` = today's, `user_language` = new), not four — and see §4.2 for why they should be **two points, not two named vectors** (zero migration, zero extra searches).
3. **Generated-vocabulary drift.** LLM-written lay phrasings can be wrong ("semantic drift by paraphrase"), and a wrong lay phrase silently misroutes every matching future query. Mitigate: keep them exploratory-only (never satisfy a required obligation via a generated phrase — the lane machinery already distinguishes required vs exploratory), sample-audit per corpus, and log which lexicon entries fire for which queries so bad cards are discoverable (the resolver already emits per-match diagnostics).
4. **Entity/mention resolution is harder than the plan implies.** The "Apple vs apple" example: `normalize_identity` lowercases and strips punctuation (`corpus_lexicon.py:195-198`), so case-based disambiguation is already gone at the identity-key level. Type-aware canonical keys (name + entity type) are needed, and the GLiNER 30-label ontology gives coarse types only. Budget real work here or scope it out of v1 — cross-document coreference ("R. B. Cialdini" = "Robert Cialdini") via embedding similarity exists in the lexicon merge, which is good enough to start.
5. **RunPod Flash extraction quality is unproven at corpus scale on this data.** The repo's own history is a warning: `extraction_contract.py:9-13` documents the 2026-07-05 collapse where a silent engine mismatch left "110/113 docs graph-dead while every screen looked green." The README's staged benchmark gates exist because of that — run them, and wire the concept-card build to `corpus_readiness` so a failed extraction can't silently produce an empty vocabulary layer (the `authentic_library` failure mode, F-item LB-6, is exactly what a silent concept-card gap would look like).
6. **No evaluation set = no way to know it worked.** The value hypothesis is lay→expert bridging. Before building, write ~50 lay-phrased queries with known expert-concept answers across two corpora (film + psychology are ideal — both live and healthy). Run them through today's pipeline for a baseline. This is a day of work that converts the whole project from vibes to a measurable delta, and it doubles as the regression suite for QW/QM/SP changes.

## 3.5 Recommended sequencing

- **Phase 0 (free):** flip on what exists — grounded planner gates (QM-1), instruction prefix (QW-6), drain summaries (QW-1). These overlap heavily with the plan's goals and cost nothing. Measure against the eval set.
- **Phase 1 (the new value, ~1–2 weeks):** LLM user-language enrichment over existing lexicon entries (salience-gated), stored as a `user_language` block on the same Mongo rows, embedded as **one additional point per enriched concept** in the same `schemas` collection (`repr: "user_language"` — see §4.2 design C for why multi-point beats named vectors here); the existing dense search returns both registers and the existing dedupe merges them; resolver marks hits with provenance. **No GLiNER required for this phase at all.**
- **Phase 2:** RunPod Flash extraction rollout through its existing benchmark gates to densify entities/relations/evidence on corpora where the legacy extractor was weak — this improves the *inputs* to Phase 1's enrichment and the graph tier.
- **Phase 3 (only if metrics demand):** additional representations (`definition_vector` split), typed graph links (broader/narrower/contrasting) upgrading `related_concepts`, entity-type-aware canonical keys.

**Bottom line on the plan:** the diagnosis (vocabulary gap) is correct, the mechanism (glosses + user-language vectors + grounded rewrites) is correct and half-built already, but the plan misassigns the generative work to an extractive model, quadruples vector storage where doubling suffices, and — as specified — would rebuild systems that exist. Reframed as *"LLM user-language enrichment of corpus_lexicon + one new named vector + resolver extension, with GLiNER densifying extraction underneath,"* it's a strong plan that lands mostly in code that already knows how to do this kind of thing.

## 3.6 Addendum — the full extraction stack: spaCy + GLiNER-ReLEx + Python orchestration

The fuller plan positions three layers, RunPod-only: spaCy as the deterministic linguistic foundation, GLiNER-ReLEx as the semantic engine, Python as the durable orchestrator, together feeding Grounded Routing with a "provable, grounded chain of evidence." The separation of concerns is architecturally correct — and it is largely how the code is *already* factored. Layer by layer against the repo:

### 3.6.1 spaCy layer — one of three claims is built

| Plan claim | Code reality | Verdict |
|---|---|---|
| Sentence segmentation anchoring every claim to sentence-level evidence | `_sentence_spans` (`app.py:186-189`), sentence-respecting window packing with offset preservation (`_windows`, `app.py:192-237`), evidence snapped to the containing sentence (`_evidence`, `app.py:240-253`) | **Built** |
| Linguistic parsing (SVO) + negation detection ("does not increase") | Default pipeline is `blank:en` + sentencizer only (`models/schemas.py:599`, `app.py:149-152`) — **no parser runs anywhere in the stack today** | **Not built** |
| Matcher rules for non-negotiables (acronyms, "AU12", product codes) | Not in the worker — but the backend already does the deterministic siblings: Schwartz–Hearst acronym pairs (`enrich.py:156-167`), casing variants (`enrich.py:170-178`), definitional-phrase cues (`enrich.py:614-637`), and a 9-FactType regex cue taxonomy (`enrich.py:48-76`), all applied to RunPod results inside `_validate_wire_result` (`runpod_flash_extraction.py:259-272`) | **Built — in the backend, not the worker** |

Two consequences worth internalizing:

**Negation is a real hole today — the plan is right to target it, and it's worse than the plan implies.** GLiNER-ReLEx will emit `X —increases→ Y` from the sentence "X does not increase Y"; the worker copies the evidence sentence verbatim, so the exact-substring gate (`evidence not in text` → drop, `runpod_flash_extraction.py:291-293`) passes it; ontology validation checks *label membership*, not polarity; and in the ghost_b LLM path the paraphrase-tolerant fallback explicitly strips negation tokens as stopwords (`"not", "no"` in `_EVIDENCE_STOPWORDS`, `ghost_b.py:682-683`) — so a polarity-flipped paraphrase can also slip through evidence validation. Nothing in the pipeline can currently see negation. **But don't parse everything to fix it.** Running a dependency parser over every window multiplies worker CPU cost on text that mostly produces no relations. The cheap, targeted design: after extraction, parse **only the evidence sentences of emitted relations** (typically a small fraction of windows), and **flag** relations whose predicate falls under a negation scope (`negated: true`) rather than silently dropping — flagged provenance stays debuggable and the graph-promotion gates can decide policy. Practical deploy note: the worker's dependency list ships spaCy but **no model wheel** (`app.py:380-387`), and `_nlp` will `spacy.load("en_core_web_sm")` only if configured (`app.py:154`) — selecting a non-blank pipeline today would crash the worker at model-load. Shipping the parser means pinning the model wheel into the Flash deploy dependencies and bumping `_CONTRACT_VERSION`.

**Decide one home for deterministic rules — it should stay the backend.** The plan puts Matcher rules in the RunPod worker; the repo already runs its deterministic linguistics in backend Python (`enrich.py`), shared across *all* engines (cloud, local vLLM, legacy sidecar, RunPod — they all pass through `_validate_wire_result`). Moving rules into the worker means paying GPU-minutes for regex, forking the logic per engine, and requiring a Flash redeploy per ruleset tweak (deploy-time contract, per `runpod_flash_extractor/README.md:37-41`). Keep the worker dumb (segmentation + GLiNER only — the things that must see raw text offsets); grow the gazetteer/Matcher layer where Schwartz–Hearst already lives. A domain gazetteer like FACS action units ("AU12") belongs in the per-corpus schema config that already flows to validation via `SchemaContext`.

### 3.6.2 GLiNER-ReLEx layer — built, with one claim to walk back

**Joint extraction: built, and already smarter than "one forward pass."** The worker runs a two-pass **entity-lens** strategy when label sets exceed the compact limit: a broad joint pass whose diluted relation output is deliberately discarded, then compact per-label-group passes for real relations (`app.py:445-485`, with the honest comment about the 0.2.27 entity-only path stalling). The plan's throughput story survives; just don't describe it as single-pass when your own worker correctly isn't.

**Zero-shot "concepts the user asks for at runtime": walk this back to ingestion-config-time.** Three code-grounded reasons. (1) The wire contract requires `entity_labels`/`relation_labels` per batch (`app.py:394-397`) mapped onto canonical ontology labels and validated against `SchemaContext.entity_vocab/relation_vocab` with soft/hard modes and sentinel fallback (`runpod_flash_extraction.py:231-235, 286-290`) — open labels are a *configuration* freedom, and that's the right shape; keep it per-corpus, not per-query. (2) The stated constraint "this method will only live in runpod extractions" already rules out query-time extraction — hold that line: RunPod Flash has 0-min-worker scale-to-zero with 60s idle timeout (`README.md:19-24`), so query-time calls would bolt seconds of cold-start onto a retrieval path that Part 1 measured at 12–40s. (3) The fine-tuned GLiREL weights embed their label strings — "the model embeds the label STRING, so the harness MUST pass these exact 30" (`incoming_glirel/README_DROP.md:33-38`) — and calibrate at a different threshold than the base model (≈0.3–0.4, not 0.5). Zero-shot label drift and fine-tuned checkpoints pull in opposite directions; pick per corpus (base model + open labels for exotic domains, fine-tuned + fixed 30 for the distilled ontology) and record which in the contract.

**Provenance anchoring: built and double-checked.** Worker drops relations without sentence evidence (`app.py:327`); backend re-verifies char offsets against text with a find-fallback (`runpod_flash_extraction.py:219-230`) and re-verifies evidence substring presence (`:291-293`). This is the strongest layer of the stack.

### 3.6.3 Python orchestrator — built, with one cheap missing gate

Durable identity and contract enforcement exist and are battle-scarred: version-gated wire contract (`app.py:391-392`), one pure contract-resolution function whose docstring documents the 2026-07-05 silent-collapse incident it was born from (`extraction_contract.py:1-23`), durable jobs, and repair-by-reconciliation as the house style (`graph_backfill.py`). The plan's "cost-effective repair" and "durable ingestion truth" are accurate descriptions of code that exists.

**The missing piece is typed relation signatures.** The plan promises "an AUTHORED relation must have a person as a source"; current validation checks that the *predicate* is in the allowed vocabulary but never checks subject/object **type compatibility** — `_validate_wire_result` verifies `subject in canonical_names` (an entity exists) and `predicate in allowed_relations`, nothing more (`runpod_flash_extraction.py:283-290`). This is a genuinely cheap, high-value addition: a domain/range table over the 30-label ontology ({predicate: (allowed_subject_types, allowed_object_types)}), enforced right there after entity typing, with soft-mode demotion to the sentinel predicate rather than a hard drop. It reuses the existing strict/soft machinery and closes the classic GLiREL failure of typed edges between mistyped endpoints.

### 3.6.4 Grounded Routing enablement — already implemented, further than even the first draft of this review assessed

The plan's "Top-Down Retrieval Scoping" ("the extraction layer has already linked concepts to specific documents and summary-tree nodes, [so] the router can reserve evidence from the most relevant sources") is not a future feature — **it is running code, end to end**:

- **Schema:** lexicon Qdrant payloads carry `source_document_ids`, `source_document_support`, `source_parent_ids`, `source_chunk_ids`, `entity_ids`, `entity_types` (`qdrant_writer.py:1417-1425`), and the resolver collects them per match (`vocabulary.py:1515-1553`).
- **Concept→document hints:** `grounded_document_route_hints` (`vocabulary.py:2288-2379`) converts a matched concept's `source_document_support` into scored document candidates (`score = 0.52 + 0.30·match + 0.12·support`, `route_source: "corpus_lexicon_provenance"`), requiring a fetched document profile for each hinted doc.
- **Reservation in the router:** `merge_grounded_document_route_hints` (`tier0_router.py:42-48` — literally docstringed *"Reserve provenance-backed documents before filling semantic slots"*) merges those hints into Tier-0 document routes, invoked in the live path at `retriever/__init__.py:1741-1755` and again for hierarchy-bound lanes at `:2082-2096`.

So the honest gap is narrower and more interesting. The reservation applies **only to system-generated expansion lanes** — `lane_lexicon_ids` is populated for `translation_*` and step-back lanes (`vocabulary.py:1811, 1869`) plus hierarchy-bound lanes (merged at `retriever/__init__.py:2077-2079`) — so a concept the user *named in their own words* (a `direct`-applicability match on a core lane) never anchors its source documents; only the system's exploratory rephrasings do. The two remaining wires are small: (1) extend the same hint path to core lanes when a lexicon match has `applicability == "direct"` — the user's own concept is the *strongest* justification for reserving its documents, stronger than a translation lane's; (2) optionally connect concept provenance to the final-packet `document_anchor_reserve` protection (`ranking_policy.py:1719-1723`), which today is a separate mechanism. Both are resolver/router integration tweaks, not extraction-stack work.

One caution stands: concept→document reservation inherits lexicon quality. Part 1 found facet noise and OCR garbage in payloads; a concept card polluted with wrong `source_document_ids` silently *mis-reserves* documents on every matching query — and because the hint path caps at 4 docs/lane and requires profile presence (`vocabulary.py:2330-2332`), a polluted card also *displaces* correct hints. The eval set (risk #6) should include cases designed to catch this (a lay phrase whose concept exists in exactly one known document).

### 3.6.5 What the fuller stack does not change

The stack — spaCy (deterministic), GLiNER-ReLEx (extractive), Python (validation) — produces exactly what it promises: a provable, grounded chain of evidence. **Every layer in it is extractive or rule-based, so §3.2's core critique stands unchanged: nothing in this stack can generate the user-language fields** (plain glosses, problem phrasings, goal phrasings, example queries) that make the vocabulary bridge bridge. "Moving naturally while talking" → "gesture–speech coupling" is a mapping no span extractor, sentence splitter, or validator can produce, because the left-hand side does not occur in the corpus. The LLM enrichment pass (§3.5 Phase 1) remains the one genuinely new build, and the extraction stack is its *input supplier*, not its implementation.

**Stack verdict:** architecture separation correct; roughly 80% built (sentence evidence, joint extraction, ontology gating, durable contracts, concept→doc reservation). Build order for the missing 20%: (1) typed relation signatures in `_validate_wire_result` — hours of work, immediate precision win; (2) post-hoc negation flagging on relation evidence sentences — closes a real correctness hole, needs the model wheel shipped in the Flash deploy; (3) extend grounded document reservation to direct-applicability core-lane concepts (§3.6.4); (4) keep deterministic rules in the backend, keep the worker dumb, keep zero-shot labels an ingestion-time per-corpus decision. And unchanged: none of this replaces Phase 1's LLM user-language enrichment — the stack feeds the bridge; it isn't the bridge.

---

# Part 4 — Follow-up Deep Dives

## 4.1 Quick Upload: is adding a file already optimized?

**Short answer: the durable design is right and the response path is nearly optimal — but it carries one real, growing lag source, and one UX gap.**

What actually happens on `POST /corpora/{corpus_id}/ingest-batches/upload` (`routers/ingestion.py:2558-2625` → `batches.py:901-1032`):

1. Read every uploaded file fully into memory (`await upload.read()`, capped at 25 files).
2. Validate extensions, then **walk the entire durable storage tree to compute the quota** — `_ensure_storage_quota` calls `_directory_size_bytes`, which does `root.rglob("*")` + `stat()` on *every file ever stored* (`batches.py:247-273`).
3. Write each file to `/data/ingest-files/<batch_id>/<item_id>.<ext>`.
4. Two Mongo writes (batch doc + item docs), then return. Parsing/chunking/embedding never blocks the response — the durable runner picks the batch up asynchronously (`_start_batch_runner_if_enabled` just stamps `run_requested_at`; in split deployments the worker discovers it by polling, `routers/ingestion.py:72-97`).

So the perceived "lag" budget is: network transfer + full-tree quota scan + disk write + 2 Mongo ops. Three findings:

- **The quota scan is the scaling bug.** It is O(total files ever stored) on *every* upload. Today it's milliseconds; after months of accumulated batches (or on `authentic_library`-scale usage) it becomes the dominant cost of the endpoint. The fix is already half-built: every batch doc records `stored_bytes` (`batches.py:984`). Replace the filesystem walk with a Mongo `$sum` aggregation over `ingest_batches.stored_bytes` (one indexed query, constant-time-ish), or maintain a single running-total counter doc updated on store/delete. Keep the walk as a weekly reconciliation job, in line with the repo's repair-by-reconciliation style.
- **Everything else is already the right shape.** Durable copy, preserved filename in metadata, batch semantics, no HTTP timeout risk on ingestion, quota guard, cleanup on partial failure (`shutil.rmtree` on exception, `batches.py:969-971`). Do not "simplify" this into a synchronous ingest — the durable batch is what makes uploads safe.
- **The UX gap is discoverability, not speed.** Quick Upload deliberately does *not* place files in `/ingest-source/...`, so later Ingest Folder / Sync passes won't see them. If the goal is "drop a file anywhere and it's in the corpus," the simplest robust option is a **watched drop folder per corpus**: a small poller (same recovery pattern the ingest worker already uses) that watches `/ingest-source/<corpus>/inbox/`, creates a folder batch for new arrivals, and moves them to a `processed/` subfolder. That reuses the existing folder-batch machinery (`create_local_batch`, `batches.py:777`) rather than adding a new upload lane. Alternatively, have Quick Upload *also* hard-link its stored copy into the corpus's source folder so Sync sees it — one `os.link` per file, effectively free.

## 4.2 Gap analysis — concept vector search: current state vs. the multi-vector plan vs. a better design

First, the mental-model correction, because the actual design choice hangs on it: **today every concept is one Qdrant point with one 1024-dim vector** (of its `embedding_gloss` text), all concepts sharing one per-corpus `schemas` collection alongside summary-tree nodes. "All concepts in the same vector" isn't the current state — but "each concept squeezed into a single vector" is, and that is the real limitation the multi-vector plan is reacting to.

### What one lexicon query costs today (measured against the code)

For each query, the resolver builds up to **8 search lanes** (`vocabulary.py:1090`), then for **every corpus × every lane** calls `search_lexicon_entries` — which internally issues **two** Qdrant round trips: a `scroll` for exact alias matches over payload indexes and a dense search over the gloss vector (`qdrant_writer.py:1611-1646`). Plus one Mongo exact/fuzzy lookup per corpus (0.8s deadline), plus document-profile fetches, plus reverse/definition-reference lookups in a second stage (`retriever/__init__.py:1905-1913`). A 4-corpus, 8-lane query is ~64 Qdrant round trips in the vocabulary stage alone — concurrent (`asyncio.gather`, `vocabulary.py:1154-1198`), but each with HTTP overhead, and all fighting the same event loop during contention. Lane embeddings, at least, are already batched upstream (`retriever/__init__.py:1481`). This is why T2 measured 1.8–18.1s.

### The three designs, compared honestly

| Design | Extra storage | Extra query cost | Migration | Quality |
|---|---|---|---|---|
| **A. Single enriched vector** — append user-language phrasings into `embedding_gloss` text, re-embed | zero | zero | none (delta re-embed already exists, `corpus_lexicon.py:2782-2947`) | Worst: lay + expert registers average into one direction; a vector can't sit close to both "the actor looks stiff" and the technical definition. Partial gain only. |
| **B. Named vectors per point** (the plan: 2–4 named vectors) | 1–3× lexicon vectors | 1 extra dense search per lane per corpus per extra vector (unless batched) | **Collection recreation** — the `schemas` collection stores a single *unnamed* vector (`PointStruct(vector=<list>)`, `qdrant_writer.py:1464-1470`); adding named vectors means rebuilding every per-corpus schemas collection, which also holds the summary-tree nodes | Best per-register precision |
| **C. Multi-point representations** — each enriched concept gets a *second point* in the same collection, same payload projection, `repr: "user_language"` payload field, vector = embedded user-language text | +1 vector per *salient* entry only | **zero** — the existing dense search returns both registers in the same top-k; no new round trips | **none** — points are just upserted; the dedupe-by-`lexicon_id` merge that already runs (`qdrant_writer.py:1656-1683`) collapses both representations to one concept, keeping the best score | Equal to B for retrieval purposes |

**Recommendation: C.** It gets named-vector quality with zero migration and zero added latency, in a codebase whose dedupe logic was already written for exactly this shape (multiple `match_type`s merging into one lexicon row). The only changes: `upsert_lexicon_entries` accepts an optional second (entry, vector) row per concept with a distinct deterministic point ID (`_schema_point_id(corpus_id, kind, lexicon_id + ":ul")`), `_lexicon_payload` gains a `repr` field, and the resolver's merge marks `match_type: "user_language_vector"` so diagnostics show *which register* matched — which is precisely the signal you want when auditing generated phrasings (risk #3 in §3.4). The doc's earlier "two named vectors" recommendation (§3.4) is hereby amended: **two representations, one collection, multi-point — not named vectors.**

### Speed fixes that apply regardless of design (ordered)

1. **Batch all lane searches into one request per corpus.** Qdrant's Query API supports batched queries (`query_batch_points`); 8 lanes × (exact + dense) can collapse from 16 HTTP round trips to 1–2 per corpus. This is the single largest vocabulary-stage win and it's mechanical: build the request list in `search_lexicon_entries`'s caller (`lane_rows`, `vocabulary.py:1126-1147`).
2. **Cache resolver output** keyed on (query hash, corpus set, lexicon epoch) — same epoch pattern as the retrieval cache (`retriever/__init__.py:763`). Conversations repeat vocabulary; today every follow-up re-pays the full stage. (= SP-3.)
3. **Skip lanes that can't match.** Lanes whose terms are all stopwords/generic already get filtered; add a cheap pre-gate: if a lane's exact-term set is empty *and* its dense text is a substring of another lane's, drop it before fan-out.
4. **Tier the corpora.** When >2 corpora are scoped, run vocabulary against the 2 with the strongest Tier-0 routing first and only fan wider if matches are weak — mirrors LB-2's shelf-first philosophy.
5. Leave HNSW/quantization alone here — 248k lexicon points is small; the cost is round trips and contention, not vector math.

## 4.3 One schema to bridge the deterministic stack and the LLM pathway

The question: if spaCy + GLiNER + Python produce extractions on RunPod, and LLMs (ghost_b cloud/local) produce them elsewhere, what schema/metadata keeps retrieval fast and both pathways compatible?

**The good news, first: the bridge exists and is load-bearing.** Both pathways already converge on one wire shape — `runpod_flash_extraction.py:4` says it plainly: the RunPod path *"returns the same validated wire shape as ghost_b_local."* Both are validated into `LLMEntity` / `LLMRelation` (`ghost_b_schemas.py:94,137` — typed `entity_type` Literal, 30-predicate Literal, evidence limits), both land in the `ghost_b_extractions` Mongo collection stamped with an `extraction_contract_hash`, and the lexicon is deliberately *"a projection, not a new extraction lane"* built from those artifacts (`corpus_lexicon.py:1-5`, `build_document_lexicon_sources` at `:848`) with per-field provenance methods already recorded (`extraction_surface_form`, `extraction_query_alias`, `extraction_definitional_phrase`, `corpus_lexicon.py:1051-1085`). The architecture you want is the architecture you have. What's missing is **parity and provenance at field level**. Concretely:

### The parity gaps (what makes the two pathways *dissimilar* today)

| Field | LLM pathway (ghost_b) | Deterministic pathway (RunPod) | Fix |
|---|---|---|---|
| `facts` (9-type taxonomy) | Emitted by SLM + deterministic pass | **Always `[]`** — worker returns none (`app.py:350`), and `_validate_wire_result` backfills aliases + definitional phrases but never calls `extract_facts`/`extract_qualitative_facts` (`enrich.py:640-648` exists but `enable_facts` is explicitly discarded, `runpod_flash_extraction.py:345`) | Run the same `enrich.extract()` deterministic fact pass on RunPod results — it's pure Python on text already in hand; makes fact coverage engine-independent |
| `definitional_phrase` | LLM emits + deterministic backfill | Deterministic backfill only | Acceptable — same field, same semantics, provenance should say which |
| `object_kind` | LLM emits, normalized against schema_lens | Always `""` (`app.py:291`) | Backfill from entity_type→object_kind mapping, or accept sparser facets and record it |
| `relation_cue` | LLM emits | Always `""` (`app.py:336`) | Post-hoc: the negation/cue parse from §3.6.1 can fill this deterministically for evidence sentences |
| Negation polarity | Nobody | Nobody | Shared `negated: bool` — one gate in `_validate_wire_result` serves **both** pathways, since both pass through it |
| Typed relation signatures | Neither enforces domain/range | Same | One table in shared validation (§3.6.3) fixes both at once |

### The unified artifact schema (concept-card-ready)

Rather than a new system, version the existing wire shape (`ghost_b_extraction.v1` → `v2`) with the fields that make retrieval faster and the pathways auditable:

```jsonc
// ghost_b_extractions row, v2 — one per (chunk, engine-run)
{
  "chunk_id": "...", "doc_id": "...", "corpus_id": "...",
  "schema_version": "ghost_b_extraction.v2",
  "extraction_engine": "runpod_flash | cloud | local | legacy_local",   // NEW: engine at row level
  "model_id": "knowledgator/gliner-relex-large-v0.5 | deepseek-chat | ...", // NEW
  "extraction_contract_hash": "...",            // exists — keep
  "entities": [{
    "canonical_name": "...", "surface_form": "...", "entity_type": "...",
    "confidence": 0.87, "char_start": 120, "char_end": 141,
    "query_aliases": ["..."], "definitional_phrase": "...",
    "provenance": {                              // NEW: field-level source
      "typed_by": "gliner | llm",
      "aliases_by": ["schwartz_hearst", "llm"],
      "definition_by": "deterministic_cue | llm"
    }
  }],
  "relations": [{
    "subject": "...", "predicate": "...", "object": "...",
    "confidence": 0.81, "evidence_phrase": "...",
    "evidence_sentence_span": [482, 561],        // NEW: spaCy span, both pathways
    "negated": false,                            // NEW: §3.6.1 gate
    "signature_valid": true,                     // NEW: §3.6.3 domain/range check
    "provenance": {"extracted_by": "gliner | llm", "cue_by": "parse | llm | none"}
  }],
  "facts": [ /* 9-type taxonomy — now REQUIRED from both engines (deterministic pass) */ ]
}
```

Downstream, the lexicon projection then adds three counters it can already compute — `evidence_count`, `doc_count` (from `source_document_support`), and `resolver_hit_count` (from vocabulary diagnostics) — which become the **salience gate** for Phase 1's user-language enrichment (§3.5) and the ranking signal for §4.2's design C.

### Why this is also the *speed* answer

Retrieval never reads `ghost_b_extractions` at query time — it reads the lexicon projection in Qdrant (payload-indexed on `canonical_key`, `member_keys`, `aliases_normalized`, `abbreviations_normalized`, `lexicon_id`, … — `qdrant_writer.py:252-265`) and Mongo. So schema unification costs queries nothing; it standardizes what flows *into* those projections so that:

1. **One validation gate serves all engines** (`_validate_wire_result` is already that gate — extend it once with signatures + negation + facts, and every current and future engine inherits it).
2. **Projections stay engine-blind.** The lexicon build doesn't care whether GLiNER or an LLM found the entity — only that the row shape and provenance are trustworthy. That's what lets you mix engines per corpus (the `extraction_contract.py` engine table) without forked retrieval logic.
3. **Field provenance enables selective trust at query time** for free: the resolver already prefers `_TRUSTED_EXACT_ALIAS_METHODS` (`vocabulary.py:53-59`) — the same pattern extends to "prefer parse-verified relation cues over LLM-asserted ones" with a payload field, no new queries.

---

# Part 5 — Closing Verdict: Does the Full Roadmap Make This a True Cross-Domain Polymath Librarian?

**About 85% — and the remaining 15% is not retrieval work.** Mapping librarian behaviors to the recommendations that deliver them:

| Librarian behavior | Delivered by |
|---|---|
| Understands what you *meant*, not what you said ("the actor looks stiff" → gesture–speech coupling) | Phase 1 user-language enrichment + multi-point representations (§3.5, §4.2) + grounded planner enabled (QM-1) |
| Knows which shelf to check, and checks it fast | Corpus routing cards (LB-2), Tier-0 cleanup (QW-5), corpus tiering (§4.2) |
| Knows the themes that cut across books | Clustered theme nodes — the restored RAPTOR layer (LB-3); graph bridge cards across corpora (LB-4) |
| Says "I don't have that" instead of handing you something | Honest answerability (QM-2), corpus-floor scale fix (LB-1) |
| Remembers what's open on the table | Conversation anchoring (QM-4), direct-concept document reservation (§3.6.4) |
| Answers at conversation speed | Warmup, batching, cache, single-stage descent (QW-3, SP-1–6): ~12–40s → ~3–8s warm, no cold cliff |
| Catalog cards that actually describe the books | Real summaries (QW-1), non-degenerate tree (QW-2), repaired/quarantined corpora (LB-6) |
| Explains why it chose those books | Retrieval rationale surfacing (LB-5) — data already computed, just shown |

**What still separates it from a *true* polymath after all of this:**

1. **Synthesis stays downstream.** The roadmap curates better evidence; the cross-domain *insight* is still produced fresh by the chat LLM at answer time. Theme nodes and bridge cards pre-compute co-occurrence-shaped connections, but there is deliberately no indexed layer that synthesizes *across* corpora — cross-corpus questions are answered by excellent juxtaposition, not pre-computed understanding.
2. **It doesn't learn from you yet.** Nothing closes the loop from "the user accepted this answer" back into routing priors, concept salience, or lexicon weights. The `resolver_hit_count` counter (§4.3) is the seed: the natural post-roadmap step is a lightweight feedback signal where accepted citations nudge document/concept priors up and ignored ones decay. That is the difference between a librarian with a great catalog and one who knows *you*.
3. **The ceiling is the catalog.** OCR garbage, facet noise, and half-ingested corpora cap quality regardless of orchestration. The hygiene items are unglamorous and load-bearing.
4. **Proof requires the eval set.** The ~50 lay-phrased golden queries (§3.4, risk 6) are what turn "feels like a librarian" into a measured claim — build them first, measure every phase against them.

Cross-domain and cross-corpus retrieval: yes, genuinely — the bones (per-corpus isolation, fair-mode blending, the graph tier, provenance discipline) were already good, and the roadmap fixes what sits on top of them. "Polymath": the system will *curate* like one; it will *think* only as well as the LLM above it, and it won't yet *grow* with use. Those two frontiers — indexed cross-corpus synthesis and interaction-driven learning — are what come after this roadmap, and both have their first footholds already named in this document.

**Later same-day addenda that sharpen this verdict:** Part 7 spells out *how* curation becomes librarian-like (role-reserved shelves, separate from `doc_artifact`). Part 8 confirms the “universal indexing contract” advice is mostly already your spine — finish/enforce it, then shelves sit on top. Part 9 locks the build to **deterministic-first** and requires shelf features to work on Fast Search without Neo4j, with Graph only adding bonuses.

---

# Part 6 — Catalog Hygiene, Stuck Deletes, Facts, and Schema/Index Gaps

Live audit (2026-07-12 evening) against Mongo + Qdrant + Neo4j. This answers: what's wrong with hygiene, where it lives, why `authentic_library` is still around after delete, whether facts/evidence classification are used, and what the schema/index layer is actually doing for curation vs retrieval.

## 6.1 What you should have vs what is live

You said there should be **3 main corpora + 1 partial**. That matches the *active* set exactly:

| Role | Name | `corpus_id` prefix | Status | Readiness | Vectors (hrag) |
|---|---|---|---|---|---|
| Main | `polymath_v2` | `999b5934` | active (legacy `status=null`) | `fully_enriched` | 278,284 |
| Main | `ecommerce_AI_FILM_SCHOOL` | `fd460347` | active | `fully_enriched` | 65,149 |
| Main | `markbuildsbrands_transcripts` | `5a20bc21` | active | `fully_enriched` | 4,344 |
| Partial | `UGO_CORPUS` | `bcf80054` | active | `graph_pending` | 861 |

Everything else in storage is leftover.

## 6.2 Why `authentic_library` is still here after you deleted it

You *did* delete it. The delete path is a **soft tombstone + background purge**, not an immediate hard wipe (`ingestion_service.delete_corpus`, `mongo_writer.delete_*`).

Live row right now:

- `name=authentic_library`, `corpus_id=f8a0aa85-…`
- `status=deleted`
- `cleanup_status=running` (stuck)
- `cleanup_warnings`: Neo4j purge failed with  
  `Neo.TransientError.General.MemoryPoolOutOfMemoryError` — transaction memory hit `dbms.memory.transaction.total.max` (~716 MiB)
- Mongo still holds **561,411 deleted chunks**, **136,081 deleted parents**, **480,212 deleted ghost_b_extractions**
- Neo4j still holds **~394,061 Fact nodes** for that corpus_id
- Qdrant per-corpus collections *were* dropped (empty / gone) — the synchronous part of delete worked
- `corpus_readiness` still has a `needs_repair` row for it

So the UI correctly hides it from `list_corpora` (`with_active_records` excludes `deleted`/`deleting`), but the **catalog body is still on disk and in Neo4j**. That is why Part 5 called hygiene load-bearing: deleted ≠ gone.

**Fix (operational, now):**

1. Force-finish the stuck purge for `f8a0aa85`:
   - Batched Neo4j delete (the current `delete_corpus_graph` OOM'd on one big `DETACH DELETE`). Need smaller batches / higher Neo4j tx memory / retry via `recover_pending_corpus_purges`.
   - Then hard-delete or archive the soft-deleted Mongo rows (today they are only `$set status=deleted`).
   - Delete the stale `corpus_readiness` row (not in the cascade at all today).
2. Patch the cascade so Neo4j failure cannot leave `cleanup_status=running` forever with `cleanup_completed_at` set — that combo is an inconsistent state.

## 6.3 Orphan catalog pollution (not just authentic_library)

These have **no active corpus row** but still occupy storage / routing:

| Remnant | Where | Size | Why it matters |
|---|---|---|---|
| `a42992d0_*` | Qdrant hrag 152,961 + schemas 44; Tier-0 117 cards; Neo4j ~9.6k facts | Large | Routes into dead docs if unscoped |
| `7c8ec461_hrag` | Qdrant 4,763; Neo4j ~4.8k facts; summary_tree row | Medium | Same |
| `0a231647-…` | Neo4j ~236k facts, Document filenames like `awesome_llms_on_device.md` | Large | Graph tier can still seed facts if a query resolves entities into this corpus_id somehow; mostly dead weight / RAM |
| Empty `corpus_*_{naive,hrag,graph,schemas}` shells | Qdrant (16a5694b, 1d43101e, 2011e31b, …) | Metadata noise | Leftover canary corpora |
| Ghost Tier-0 cards | `polymath_doc_summaries` (858 total; only ~690 belong to the 4 live corpora) | Routing pollution | Probe junk titles ("tier0 probe note") still present |
| `summary_tree` rows for deleted IDs | Mongo | Stale hierarchy | `retire_corpus_derived_state` didn't cover every historical ID |

**One-shot sweeper needed** (admin script, not a product feature):

1. List active corpus_ids from `corpora` where status is missing/active.
2. Drop every `corpus_<prefix>_*` Qdrant collection whose prefix ∉ active set.
3. Scroll-delete `polymath_doc_summaries` points whose `corpus_id` ∉ active set.
4. `MATCH (n {corpus_id:$dead}) DETACH DELETE n` in batches for dead Neo4j corpus_ids (especially `f8a0aa85`, `0a231647`, `a42992d0`, `7c8ec461`).
5. Delete `summary_tree` / `corpus_readiness` / soft-deleted Mongo rows for dead ids (or move to an archive DB).

Until that runs, "3 + 1" is a UI fiction over a much larger physical catalog.

## 6.4 Hygiene inventory — what's wrong, where it lives, how to fix

| Defect | Where it lives | What it does to retrieval | Fix |
|---|---|---|---|
| Placeholder summaries (`summary_text == chunk_text`, empty `summary_model`) | Qdrant `*_hrag`/`*_naive` `chunk_type=summary`; written in `qdrant_writer.py` (~862), contract in `summary_semantics.py` | Funnel A searches near-duplicates of children | QW-1: filter empty `summary_model` in `funnel_a.py`; stop upserting placeholders; drain heal jobs |
| Degenerate section=rollup trees | Mongo `summary_tree`; navigator in `summary_tree_navigator.py`; build in `summary_tree.py` | Extra 1.5–7s routing for no info | QW-2: collapse 1:1 section/rollup; ignore `Page N` headings |
| Separator-only / low-signal chunks | Ingest: `tier_chunker.py` (has `is_separator_only_text`); retrieval also drops some in `__init__.py` | Can still win rerank (T9 dashes) | Tighten ingest drop; keep retrieval guard; extend beyond pure separators to low-alpha OCR lines |
| OCR / garbage heading paths | `tier_chunker.py` OCR AST path; parent `heading_path` | Pollutes topic_key / routing text | Reject/normalize garbage headings at ingest; don't derive `topic_key` from them |
| Facet / concept noise | Promote payload `concepts[]` (`promote.py`); soft-prefilter in `funnel_b.py:107-129` | Soft `should` on noisy concepts can skew candidate pools; cross-domain tags bleed | Salience gate on promote; drop 1-char/generic concepts; prefer lexicon-canonical names |
| Empty `domain` field | Payload `domain` present but empty on sampled live points; used by `cross_domain.py` | Domain-reserve swap is inert | Fill domain from summary_semantics / schema_lens at ingest; don't index empty strings |
| Weak promote coverage on `polymath_v2` | Only ~14/100 sampled naive points have non-empty `concepts`; `semantic_chunk_type`/`mechanisms` ≈ 0 | Film/mark corpora are healthier; polymath_v2 under-uses schema filters | Re-run `promote_doc` / promote backfill over polymath_v2 |
| Soft-deleted tombstones | Mongo `chunks`/`parent_chunks`/`ghost_b_extractions` with `status=deleted` | Hidden from reads, still consume disk and confuse audits | Periodic hard-delete after purge complete; archive optional |
| Stuck / partial corpus purge | `corpora.cleanup_*`; Neo4j graph | Dead libraries keep facts alive | Batched graph delete + sweeper (§6.2–6.3) |
| Collection triplication | `*_naive` + `*_hrag` + `*_graph` | 3× write/RAM | SP-4 single collection + `chunk_type` filter |

## 6.5 Are facts used with evidence classifications?

**Yes, facts are used — but only on the graph tier, and "evidence classification" is basically `fact_type`, not a separate evidence taxonomy.**

Pipeline:

1. **Ingest:** Ghost B / enrich write `:Fact` nodes with `fact_type` ∈ `{property, category, status, quantity, timestamp, threshold, tag, rule_condition, rule_action}` plus `evidence_phrase`, confidence (`enrich.py` CUES; Neo4j writer).
2. **Promote:** `fact_types[]` is projected onto chunk payloads (`promote.py:107-117`) so vector search *could* filter by fact class.
3. **Retrieve (graph tier only):** `_retrieve_graph_seed_facts` → `fact_retrieval.retrieve_facts_for_entities` (`fact_retrieval.py`). Facts **bypass the reranker** and go into the prompt as key facts with `evidence_phrase`.
4. **Ranking among facts:** semantic rank prefers definitional `property` names, then `category`/`status`, then rules, then numeric/timestamp (`fact_retrieval.py:82-95`). Caller currently passes `fact_types=None` (`retriever/__init__.py:970`) — so **all types are eligible**, sorted by that rank, not intent-filtered.
5. **Fast / Hybrid tiers:** no Neo4j facts. `fact_types` on Qdrant payloads are barely used as a hard filter today; Funnel B's soft prefilter is `concepts` / `entity_ids`, not `fact_types`.

Live Neo4j inventory: **951,607 facts** — but ~394k belong to deleted `authentic_library` and ~237k to orphan `0a231647`. So a large fraction of the "fact catalog" is dead-corpus residue.

**Gaps to close:**

- Intent-route `fact_types` (definitional → `property`/`category`; numeric → `quantity`/`threshold`) instead of always `None`.
- Use payload `fact_types` in Funnel B soft `should` for hybrid when graph is off.
- Require non-empty `evidence_phrase` at promotion time (already mostly true on graph path).
- Purge dead-corpus facts (§6.2) so graph seeding cannot waste budget on ghosts.

## 6.6 Schema storage & indexes — how curation vs retrieval actually use them

### Storage layout

| Store | What | Indexed how | Used at query time |
|---|---|---|---|
| Qdrant `*_naive` / `*_hrag` / `*_graph` | Children + summaries (triplicated) | Dense (+ sparse on modern); payload indexes: `corpus_id, doc_id, parent_id, chunk_type, chunk_kind, concepts, entity_ids, domain, …` (`qdrant_writer.py:222-251`) | Funnel A: summaries; Funnel B: children + soft `concepts`/`entity_ids`; `must_not` NOISY_KINDS |
| Qdrant `*_schemas` | Lexicon cards **and** summary-tree nodes (mixed `kind`) | Payload: `canonical_key, aliases_normalized, lexicon_id, node_id, …` (`:252-265`) | Vocabulary exact+gloss search; tree navigation |
| Qdrant `polymath_doc_summaries` | Tier-0 document cards (shared) | Vector + corpus/doc payload | Document-first routing |
| Mongo `chunks` / `parent_chunks` / `summary_tree` | Bodies, hierarchy, jobs | Active-status filter | Hydration, tree fallback |
| Mongo `corpus_lexicon` | Concept cards | Gloss texts + provenance | Vocabulary / grounded planner |
| Neo4j Entity/Fact/RELATES_TO | Graph catalog | `fact_type`, entity_id constraints | Graph-tier facts + decoration |

### What works

- Identity indexes (`corpus_id`, `doc_id`, `parent_id`, `chunk_type`, `chunk_kind`) make scoped retrieval fast.
- Lexicon exact-alias indexes make vocabulary bridging possible without embeddings.
- Promote → `concepts`/`entity_ids` soft prefilter is the right idea (with unfiltered fallback).

### What is broken or incomplete for quality + speed

1. **Catalog residue** (§6.2–6.3) — largest practical quality tax after placeholders.
2. **Promote unevenness** — `markbuildsbrands`/`film` have rich `semantic_chunk_type`/`mechanisms`/`key_terms`; `polymath_v2` mostly doesn't. Cross-domain levers in `cross_domain.py` stay cold when `domain` is empty.
3. **Schemas collection mixing** lexicon + tree nodes — fine with `kind` filter, but makes named-vector migrations painful (see §4.2) and complicates orphan sweeps.
4. **Triplication** — same child in three collections; retrieval only needs one.
5. **Fact-type / semantic_chunk_type underused at query time** — ingested and sometimes indexed, rarely driving lane choice.
6. **Ingest should add (minimum set):**
   - Real parent summaries (not placeholders) + `summary_model`
   - Non-empty `domain` + clean `topic_key`
   - Promote backfill (`concepts`, `entity_ids`, `fact_types`, `semantic_chunk_type`, `mechanisms`, `key_terms`)
   - Separator/OCR rejection before embed
   - User-language block only for salient lexicon entries (§4.2 design C)
   - Negation flag + relation signatures on extraction validation (§3.6)
   - Hard readiness gate: corpus not queryable until vectors + tree + promote coverage clear thresholds (UGO correctly shows `graph_pending`; authentic_library should have been non-queryable, not "silent empty")

### Priority order if you only do three things this week

1. **Finish authentic_library purge + orphan sweeper** (Neo4j batched delete, drop dead Qdrant collections, scrub Tier-0).
2. **Promote backfill + placeholder summary filter** on `polymath_v2` (biggest live catalog).
3. **Wire fact_type intent routing** on graph tier + keep Fast/Hybrid honest about having no facts.

After that, the catalog stops fighting the orchestration — and the Part 2/4 roadmap gains can actually show up in eval numbers.

---

# Part 7 — Librarian Shelves (Role-Reserved Curation)

**Status:** analysis only (no implementation in this session). Complements Part 2’s LB-* items: those improve routing/fairness/themes; this section defines the **growth-story librarian** behavior — curating a mixed shelf packet instead of eight near-duplicate Direct hits.

## 7.1 The target behavior

Query (story, not keyword bag):

> “I want to be wiser while also being more confident with women”

Desired outcome is a **packet of books/roles**, not eight relationship excerpts:

| Shelf | Job | Example |
|---|---|---|
| **Direct** | Explicitly addresses the goal | relationship / social-confidence books |
| **Foundational** | Identity, meaning, habits, judgment | *Man’s Search for Meaning*, *Atomic Habits*, *Judgment under Uncertainty* |
| **Adjacent** | Same capabilities, related discipline | persuasion / power dynamics (*48 Laws*), attraction psychology |
| **Bridge** | Non-obvious transfer path with **evidence** | *Strategy and Tactics of Pricing* → value framing / high-value positioning |
| **Counterbalance** | Challenges misuse / shallow reads | ethics, anti-manipulation, bias, humility (*Art of War* as strategy discipline, not “dominate”) |

Final top-k must **reserve seats across shelves**, or MMR collapses everything into Direct. Pricing belongs only if it wins a **Bridge** seat via a stored transfer path — never because it ranked #3 on the literal string “women.”

## 7.2 Critical constraint: do not reuse `doc_artifact`

Existing `doc_artifact.v1` (`backend/services/ingestion/doc_artifact.py` + Continuity brief) is explicit:

> Artifacts **explain** retrieved sources; they **never choose** them.

It is for synthesis framing (model-specific vs theory vs workflow; video-model registry, etc.). Librarian shelves are the opposite: they **must** choose / reserve documents. Implement as a **separate** card — e.g. `librarian_card` / `book_card` under `documents.doc_profile` — used by Tier-0 + ranking, **not** by the synthesis-header path in `assembly.py` / `hydrate.py`.

## 7.3 Ingestion: universal book-card fields

**Today’s Tier-0 card is thin.** `doc_profile` is roughly:

- `summary` (“what it is / best used for…”)
- `concepts[]`
- `domains{name:count}`
- `section_ids`
- optional passive `doc_artifact`

Built in `summary_tree.py` (`_PROFILE_PROMPT`) and embedded into `polymath_doc_summaries` by `tier0.py` (title/summary/concepts).

**Upgrade schema (universal — same fields for every corpus):**

```text
central_subjects
problems_addressed
capabilities_developed
mechanisms_taught
transferable_principles
situations_where_applicable
prerequisites
risks_or_likely_misuse
counterbalancing_concepts
evidence_spans[]   # section/parent/chunk ids per claim
```

**Fill order (truthful):**

1. **Deterministic / extractive first** from existing signals:
   - `mechanisms_taught` ← parent `mechanisms` / promote (`summary_semantics.py`, `promote.py`)
   - `central_subjects` ← lexicon + Ghost B entities + profile concepts
   - `problems_addressed` / `capabilities_developed` ← only source-backed (lexicon definitions, “useful for”, `application_contexts`)
2. **LLM structured fill** with `summary_semantics.py` discipline: JSON contract, caps, provenance, extractive fallback
3. Every claim needs `evidence_spans` — otherwise Bridge/Counterbalance become vibes

**Index shape:**

- Tier-0 embed text = a **routing gloss** from card fields (subjects + problems + capabilities + principles), not one mega-blob.
- Prefer multi-point / multi-repr later (same lesson as §4.2): e.g. problems/capabilities vs principles/mechanisms — not four named vectors on day one.
- Full card on Mongo `documents.doc_profile.librarian_card`; retrieval-useful subset on Qdrant Tier-0 payload.

## 7.4 Query time: shelf obligations on QueryPlanV2

**Today:** `build_query_plan_v2` builds phrase/concept **lanes**. Intent is only `BROAD|BALANCED|SPECIFIC` (`intent_policy.py`). Ranking reserves by corpus / graph / document-anchor — **not** by intellectual role (`ranking_policy.py`).

**Needed:** shelf obligations beside lanes:

```text
direct_shelf         required
foundational_shelf   required for growth/identity goals
adjacent_shelf       exploratory
bridge_shelf         exploratory, must cite transfer path
counterbalance_shelf required when Direct includes power/persuasion/attraction
```

Story → capabilities (wiser → judgment/meaning/bias; confident → social ease/self-worth/framing) is closer to **grounded_planner + vocabulary** than pure phrase lanes:

- User language → corpus concepts (`vocabulary.py`)
- Concepts → documents via `source_document_ids` / Tier-0
- Shelf labels by **matching query capabilities to card fields**, not filename guesses

Do **not** assign shelves only in the LLM answer step. If seats aren’t reserved before final top-k, you still get eight Direct chunks.

## 7.5 Retrieval: two-stage librarian pass

**Stage A — Book selection (Tier-0++, shelf-aware)**

1. Match against librarian-card routing glosses
2. Score docs into shelves via field overlap:
   - Direct: `problems_addressed` / `situations_where_applicable` hit goal
   - Foundational: capabilities in {meaning, habits, judgment, emotional_regulation, identity}
   - Adjacent: shared `capabilities_developed` / `mechanisms_taught`, different `central_subjects`
   - Bridge: shared mechanism/principle + explicit evidence, low lexical overlap with Direct
   - Counterbalance: `risks_or_likely_misuse` / `counterbalancing_concepts` intersects Direct subjects
3. Reserve N docs per shelf (e.g. 2/2/2/1/1) before child descent

**Stage B — Evidence within reserved books**

Existing tree → parent → child path stays. Diversity becomes **cross-book / cross-shelf**, not MMR on similar relationship paragraphs.

## 7.6 Final packet: `shelf_reserve` (like `corpus_floor`)

Mechanical pattern already exists in `ranking_policy.py`:

- `document_anchor_reserve`
- `graph_reserve`
- `corpus_floor`
- protected reasons that can’t be casually replaced

Add **`shelf_floor` / `shelf_reserve`**:

- After MMR, ensure each required shelf has ≥1 seat when candidates exist
- Never seat Bridge without a stored transfer path (mechanism/principle + evidence ids)
- Never leave Counterbalance empty if Direct is high-misuse (power, manipulation, seduction)
- Diagnostics: which shelf seated which doc → librarian rationale line (“X for confidence, Y for judgment, Z because value-framing transfers”)

Without this, the architecture fails in the last 20 lines of ranking (same class of bug as F5’s corpus-floor scale mismatch).

## 7.7 What already helps vs what’s missing

| Building block | Status | Role |
|---|---|---|
| Tier-0 doc cards | Exists, thin | Entry — needs `librarian_card` fields |
| Parent mechanisms / semantic types | Exists unevenly | Seed `mechanisms_taught` |
| Lexicon + grounded planner | Exists | Map story language → concepts |
| Concept→doc reservation | Exists for expansion lanes | Extend to Direct + shelf docs (§3.6.4) |
| `corpus_floor` / `graph_reserve` | Exists | Template for `shelf_reserve` |
| Cross-domain mechanism bonus | Exists, often inert (`domain` empty) | Adjacent/Bridge signal |
| `doc_artifact` | Exists | **Not** the shelf chooser — synthesis only |
| Theme / RAPTOR clusters (LB-3) | Missing | Later accelerator for Foundational/Adjacent |
| Shelf roles in QueryPlanV2 | **Missing** | Core new planning object |
| Shelf reservation in ranking | **Missing** | Core new selection constraint |
| Counterbalance as first-class | **Missing** | Card field + reserve rule |

## 7.8 Sequencing (when implementing)

1. Schema + ingest contract for `librarian_card` (universal fields + evidence spans + provenance)
2. Backfill on the 3 main corpora from summaries/mechanisms/lexicon; LLM only for gaps
3. Tier-0 payload/embed upgrade to card glosses
4. QueryPlan shelf obligations for growth-style intents (goal language / answer shape, not only BROAD)
5. `shelf_reserve` in `ranking_policy` with diagnostics
6. Only then Bridge scoring that can seat pricing-like books without contaminating Direct

**Eval set for this metaphor (required before claiming success):**

- Goal/growth queries (“wiser + confident with women”)
- Must include ≥1 Foundational and ≥1 non-Direct Adjacent/Bridge when corpus contains them
- Must include Counterbalance when Direct is power/attraction
- Must **not** flood with eight relationship chunks
- Bridge must show an explicit transfer reason in diagnostics

**Bottom line:** a polymath librarian is a **role-reserved curator**, not a better similarity search. Hooks exist (doc cards, vocabulary bridging, reserve passes). Missing pieces: source-grounded universal book card + shelf-aware plan + final-seat policy. Universal indexing (Part 8) makes books comparable; shelves decide which **roles** get seats.

---

# Part 8 — Universal Indexing Contract (Grounded Against This Repo)

External advice summarized: *use a versioned universal indexing contract — file type and corpus affect configuration, not custom retrieval code; every source yields the same artifact types; domain terms are values inside a universal schema; drain hand aliases out of Python; enforce readiness publication gates.*

**Verdict: mostly true as architecture, and largely already Polymath’s own doctrine.** It is not a foreign redesign. A few claims are overstated or point at the wrong file; a few “next steps” are already built but unevenly enforced. Universal indexing is **necessary but not sufficient** for Part 7’s librarian feel.

## 8.1 Claim-by-claim against the codebase

| Claim | Against this repo |
|---|---|
| One universal pipeline: profile → tree → parent/child → summaries → concepts → graph → indexes → readiness | **True as doctrine.** Ingest already aims at this spine. |
| Domain terms are values, not schema fields | **True as intent**, violated in places (hand aliases, facet noise, fixed domain taxonomy). |
| File/corpus config, not custom retrieval code | **Mostly true.** Retrieval is corpus-scoped and tiered; remaining customization is data/heuristics, not `if corpus == film`. |
| Readiness gates (queryable vs fully enriched) | **True and implemented** in `readiness.py` (`corpus_readiness.v2`). |
| Move aliases out of `facets/normalizer.py` | **Half-wrong target.** That file is mostly deterministic tokenization / stable facet IDs. The real hand-authored content-alias lump is `CONCEPT_ALIASES` in `query_semantics.py` (+ relation alias maps elsewhere). |
| Temporal index (`published_at` / `valid_until`) as first-class retrieval | **Not really there** for corpus docs. Freshness exists mainly for **web** (`web_freshness.py`). |
| Separate gloss + utility vectors as separate points | **Directionally right**; today lexicon is **one gloss vector per concept** (plus text fields). Grounded design = §4.2 multi-point. |
| Most can be backfilled without re-parse | **True** for lexicon/promote/readiness/doc profiles; **false** for hollow summaries / missing trees / dead-corpus residue (Part 6). |

## 8.2 What you already have (the advice is describing your spine)

**Universal artifact spine — real**

- Document profile + Tier-0: `summary_tree.py`, `tier0.py` → `polymath_doc_summaries`
- Hierarchy: `summary_tree` + parent/child in Mongo/Qdrant
- Evidence index: dense (+ sparse) children in per-corpus collections
- Vocabulary: `corpus_lexicon.py` + Qdrant `schemas` lexicon points
- Graph: Neo4j entities/relations/facts via Ghost B / RunPod → promote
- Readiness: `readiness.py` statuses (`queryable_*`, `summaries_pending`, `lexicon_pending`, `graph_pending`, `fully_enriched`)

**Same wire for engines — real**

- RunPod Flash validates into the same `ghost_b` shape (`runpod_flash_extraction.py`)
- Lexicon is explicitly a **projection over extractions**, not a parallel schema (`corpus_lexicon.py`)

**Corpus owns policy, not domain physics — mostly real**

- Corpus carries ownership, extraction engine contract, neo4j flag, ingest profile
- Domain knowledge is supposed to live in lexicon/graph **values** with `corpus_id` provenance

## 8.3 Where the advice correctly names your gaps

**1. Hand-authored concept customization still lives in code**

Not mainly `facets/normalizer.py`. Clearer example — `CONCEPT_ALIASES` in `query_semantics.py` (~527+): ecommerce/product aliases (`abandoned_cart`, `conversion`, `customer_acquisition`, `offers`, …) frozen in Python. That is “customizing for a period/domain in code.” Lexicon + Schwartz–Hearst + extraction aliases are the data-driven replacement — but query planning still also uses the frozen dict. Ontology labels as code config are fine; **content aliases as code** should drain into versioned lexicon data. Same class: Ghost B / `schema_lens` relation alias maps (`RELATION_ALIAS_MAP`, `_RELATION_ALIAS_TO_APPROVED`).

**2. Facets / domains still behave like a closed taxonomy**

`ghost_a._DOMAIN_TAXONOMY` is a fixed list. Facets stamp shared tags that bleed across books (F8: `agency_preservation`, `emotional_patterns` on cinematography and Resolve). Live audit: empty `domain` on many payloads, noisy `concepts`. “Mine terminology → normalize into universal concept schema” is the right fix — partially done via promote/lexicon, unevenly (`polymath_v2` weaker than film/mark).

**3. Summaries are not yet “only model-attributed, evidence-backed”**

Contract exists (`summary_semantics.py`); live state violates it (F1 placeholders, empty `summary_model`). Enforcement at write/read is incomplete (QW-1).

**4. Readiness exists but physical catalogs still lie**

UGO correctly shows `graph_pending`. `authentic_library` was UI-deleted with residue (Part 6). Publication contract is real in code, not fully true in ops.

**5. One concept → many representations**

Advice’s “embed gloss + functional-use as separate points” matches §4.2 design C (`repr: user_language` multi-point), not a new concept system.

## 8.4 Where the advice oversells or misses

- **“Doesn’t feel customized”** — worst form (`if corpus_id == film`) is already avoided. Remaining feel comes from heuristic aliases in Python, video-centric `doc_artifact` registry (intentional product use), and **data maturity** differences across corpora — not forked retrieval code paths.
- **Temporal index** — not load-bearing for corpus docs today; adding `published_at` / `valid_from` / `valid_until` / `temporal_class` would be new work.
- **“Build concept cards from Mongo”** — already true (`build_document_lexicon_sources` from `ghost_b_extractions`). Missing = richer user-language / utility reps + Part 7 shelf fields — not start-from-zero.
- **Held-out naive/technical/broad/cross-domain eval** — still underbuilt (live probes ≠ durable harness).
- **Librarian shelves** — universal indexing alone does not produce Direct/Foundational/Bridge packets. Indexing makes every book comparable; **shelves** decide roles (Part 7).

## 8.5 Grounded reading of the “practical next direction”

Mapped to this repo, honest order:

1. **Drain `CONCEPT_ALIASES` (and similar) into lexicon/data** — yes; target `query_semantics.py`, not primarily facet normalizer.
2. **One document/node/concept/evidence contract across engines** — mostly done; finish parity (facts on RunPod path, negation/signatures, field provenance — §3.6 / §4.3).
3. **Concept cards from Mongo** — already; enrich + multi-repr (gloss + user/utility), don’t rebuild.
4. **Same readiness validator everywhere** — exists; enforce + finish purges so “queryable” means physically clean (Part 6).
5. **Held-out eval set** — yes, before more tuning (also Part 7 growth-story cases).
6. **Temporal as metadata** — good future add; not current load-bearing path.
7. **Then** Part 7 `librarian_card` + `shelf_reserve` — universal catalog → role-reserved curation.

## 8.6 Universal Identity / Structure / Semantics / Trust (how it maps)

External four-class metadata map:

| Class | Already present (examples) | Gaps |
|---|---|---|
| **Identity** | `corpus_id`, `doc_id`, `parent_id`, `chunk_id`, content hashes / stage identity in readiness | Orphan IDs after soft-delete; ghost Tier-0 cards |
| **Structure** | `chunk_type`, heading path, tree node kinds, source order | Degenerate 1:1 section/rollup; Page-N sections |
| **Semantics** | `semantic_chunk_type`, mechanisms, lexicon concepts/aliases, fact_type | Uneven promote; empty `domain`; hand `CONCEPT_ALIASES`; no librarian_card fields yet |
| **Trust** | extraction engine / contract hash, evidence phrases, confidence, readiness status | Placeholder summaries with empty `summary_model`; negation flag missing; temporal scope weak |

Domain-specific concepts (FACS, Meta Ads, Laban) must remain **values** discovered into this schema — not new schema columns or Python `if "stress"` facet forks.

## 8.7 Bottom line

The external answer is **true as architecture** and largely **describes Polymath’s existing contract**. What’s false-ish: implying a greenfield build, or that `normalizer.py` is the main alias problem. What’s actionable: stop putting domain knowledge in Python dicts; enforce summary/vocabulary/graph readiness as hard publication gates; keep schema universal; extend `corpus_lexicon` with multi-representation points (§4.2); then layer Part 7 shelves so the catalog can answer growth-style stories without collapsing into eight Direct hits.

---

# Part 9 — Deterministic-First Librarian Replan (Tier-Grounded)

**Constraint (product decision):** build the librarian as **universal + deterministic first**. LLM may later enrich cards, thin story→capability mapping, and explain seated paths — it must **never** choose seats. This section replans Parts 7–8 against the real three UI routes and their store contracts in code.

## 9.1 Why this constraint improves the plan

| Without the constraint | With deterministic-first |
|---|---|
| Blocked on LLM card quality before any seats work | Seats ship from promote/lexicon/tree fields already in the catalog |
| Bridge = generative story (hard to eval, easy to hallucinate) | Bridge = shared principle/mechanism id + evidence spans |
| One “smart” path that secretly needs Graph | Same shelf policy on Fast/Hybrid/Graph; Graph only *adds* stores |
| Tier tests become afterthoughts | Every phase must pass `three_tier_eval` / store-contract invariants |

This matches engineering invariants in `AGENTS.md`: evaluate every retrieval change across Focused (Fast), Hybrid, and Graph Augmentation, and **do not violate storage boundaries** to make one test pass.

## 9.2 The three tiers as they actually exist

Enums: `RetrievalTier.qdrant_only` | `qdrant_mongo` | `qdrant_mongo_graph` (`models/_schemas_legacy.py`). UI names and purposes: `three_tier_eval.ROUTES`. Observable contract: `_retrieval_store_contract` in `retriever/__init__.py` (asserted in `tests/test_hybrid_lexical_retrieval.py`).

| UI route | Tier value | Allowed stores | Explicitly forbidden |
|---|---|---|---|
| **Fast Search** | `qdrant_only` | Qdrant dense + sparse/RRF; child + summary vectors; optional cross-encoder | Mongo lexical (`_lexical_limit_for` → 0); document-title anchors (`_document_anchor_limit_for` → 0); Neo4j facts/expansion |
| **Hybrid Search** | `qdrant_mongo` | Everything Fast uses **plus** Mongo lexical recall, Mongo hydration, document-title anchors | Neo4j facts; Mode A expansion; `graph_reserve` |
| **Graph Augmentation** | `qdrant_mongo_graph` | Everything Hybrid uses **plus** Neo4j fact seeding (`_retrieve_graph_seed_facts`), Mode A expansion (`mode_a_expansion`), graph decoration/metrics; vocabulary may attach Neo4j neighbors | Using Neo4j when strategy intersection fails (downgrade to Hybrid) |

**Shared across all three (already):** QueryPlanV2, vocabulary Qdrant lexicon search, Tier-0 `polymath_doc_summaries` routing, Funnel A/B (with fair-mode A skip on multi-corpus), `select_with_diversity` / `corpus_floor`, rerank as an independent policy (`_rerank_enabled_for_tier`).

**Tier-aware ranking already in `ranking_policy._mmr_policy_for`:**

| Tier | MMR λ (base) | Relevance floor | Extra reserve |
|---|---|---|---|
| Fast | 0.75 | 0.90 (tight — fights vector-neighborhood collapse) | none |
| Hybrid | 0.65 | 0.35 BROAD / 0.85 SPECIFIC | none |
| Graph | 0.55 | 0.35 BROAD / 0.80 SPECIFIC | `graph_reserve` 1–2; `max_same_predicate=3` |

**Strategy intersection** (`_enforce_strategy_intersection`): Graph requires `use_neo4j=True` on **all** selected corpora; else effective tier becomes Hybrid. UGO/`graph_pending` corpora therefore must not be assumed Graph-capable for librarian features that need Neo4j.

**Vocabulary store usage** (`vocabulary.py`): Qdrant lexicon on all tiers; Mongo fuzzy/exact only Hybrid+Graph; Neo4j neighbor attach only Graph.

## 9.3 Hard rule for librarian shelves across tiers

```text
Shelf selection inputs MUST be available on Fast Search.
Graph may enrich Bridge/Counterbalance; it must not be required for seats.
```

| Librarian mechanism | Store home | Fast | Hybrid | Graph |
|---|---|---|---|---|
| `librarian_card` fields on Tier-0 payload | Qdrant `polymath_doc_summaries` (+ Mongo `doc_profile`) | ✓ | ✓ | ✓ |
| Capability overlap scoring | In-process over Tier-0 hits + card payload | ✓ | ✓ | ✓ |
| `shelf_reserve` (like `corpus_floor`) | `ranking_policy.select_with_diversity` | ✓ | ✓ | ✓ |
| Bridge gate: shared principle/mechanism + `evidence_spans` | Card payload / lexicon ids pointing at parent/chunk ids | ✓ (Qdrant ids) | ✓ (+ hydrate text) | ✓ |
| Transfer edges as **joins on shared lexicon ids** | Mongo/Qdrant (offline materialization) | ✓ | ✓ | ✓ |
| Transfer edges via Neo4j `RELATES_TO` / multi-corpus entities | Neo4j | ✗ | ✗ | ✓ bonus only |
| Counterbalance via versioned misuse/counterbalance **key sets** | Data file + card subjects | ✓ | ✓ | ✓ |
| Counterbalance via graph-typed contrast relations | Neo4j | ✗ | ✗ | ✓ bonus only |
| Story → capabilities via lexicon Qdrant | `schemas` lexicon points | ✓ | ✓ | ✓ |
| Story → capabilities via Mongo lexicon fuzzy | Mongo | ✗ | ✓ | ✓ |
| Fact-seeded “principle” boost into Bridge scoring | Neo4j facts | ✗ | ✗ | ✓ bonus only |
| LLM seat picking | — | **Forbidden on all tiers** | | |

If a growth query on Fast cannot seat Foundational/Adjacent from card overlap alone, that is a **card/index gap**, not a reason to call Neo4j from Fast.

## 9.4 Where shelves plug into the existing retrieve pipeline

Current planned flow (docstring at top of `retriever/__init__.py`): strategy intersection → (Graph: fact seeds) → embed → Funnel A/B (+ lexical/anchors on hydrated tiers) → merge → (Graph: Mode A) → rerank → trim → hydrate.

**Deterministic librarian inserts (all tiers):**

```text
[plan + vocabulary]
    → capabilities = lexicon-resolved concept keys (no LLM required in v1)
    → Tier-0 document recall (existing)
    → NEW: score each routed doc into shelves via card field overlap
    → NEW: shelf_doc_reserve — pin N docs/shelf before child descent budgets
[funnels / lexical / graph as tier allows — but only inside reserved docs when shelf mode on]
    → merge / rerank
    → NEW: shelf_reserve in select_with_diversity (final packet seats)
    → diagnostics: shelf, matched_fields, evidence_span_ids
```

**Tier-specific behavior after the shared insert:**

- **Fast:** no Mongo lexical rescue if card overlap is weak; rely on Qdrant Tier-0 + child/summary vectors. Tight relevance floor stays — do not weaken Fast floors to force Bridge seats.
- **Hybrid:** lexical + document-anchor can recover exact titles/terms for reserved books; hydration makes Bridge evidence readable. Still no Neo4j.
- **Graph:** after Hybrid-equivalent pool, fact seeds / Mode A / `graph_reserve` may **add** Bridge or Counterbalance *candidates* when entities/relations link principles across docs — then the same `shelf_reserve` decides seats. Graph must not dump unbounded facts into Direct and starve other shelves (`max_same_predicate` already fights fact spam).

**Downgrade honesty:** if the user asked Graph but intersection downgrades to Hybrid, shelf diagnostics must record `effective_tier` and that Neo4j Bridge bonuses were skipped — same pattern as today’s `store_contract` on the result.

## 9.5 Ingestion (universal, deterministic) under this replan

One card schema for every corpus (Part 7 fields). **v0 fill = projections only:**

| Field | Deterministic seed |
|---|---|
| `central_subjects` | lexicon / Ghost B entities / profile concepts → `canonical_key` |
| `mechanisms_taught` | promote / `summary_semantics` mechanisms |
| `transferable_principles` | high multi-doc lexicon concepts |
| `capabilities_developed` / `problems_addressed` | extractive from lexicon application/definitional surfaces only; else empty |
| `evidence_spans` | existing `source_parent_ids` / `source_chunk_ids` / tree node ids |
| `risks_or_likely_misuse` / `counterbalancing_concepts` | empty in v0, or only if already extractable |

**Publish rules (readiness-adjacent):**

- No span → field value not written  
- Card incomplete → shelves degrade to today’s Tier-0 (fail-open), never invent roles  
- Graph promotion remaining (`graph_pending`) does **not** block Fast/Hybrid shelf use of Qdrant/Mongo card fields  

Offline **transfer_edge** materialization (Phase 3): join docs that share a principle/mechanism id but differ in `central_subjects`; store edge with evidence ids in Mongo (readable on all tiers). Neo4j multi-corpus `RELATES_TO` is an optional Graph-tier *additional* candidate source, not the edge system of record for Fast.

## 9.6 Phased build order (replanned)

### Phase 0 — Catalog trust (all tiers benefit)

Placeholder summary filter (QW-1), orphan/`authentic_library` purge (Part 6), promote backfill on `polymath_v2`. Without this, Fast’s summary lane and Hybrid soft `concepts` prefilter encode noise into shelf scores.

### Phase 1 — `librarian_card` v0 + Tier-0 payload

Writer + payload indexes for subject/capability/mechanism keys. No LLM. Verify card presence via readiness diagnostic (separate from `fully_enriched` if needed: `shelf_card_ready`).

### Phase 2 — Query: overlap scorer + `shelf_reserve`

- Capabilities from vocabulary Qdrant (works on Fast); Hybrid/Graph may add Mongo fuzzy hits  
- `shelf_reserve` beside `corpus_floor` / `graph_reserve` in `ranking_policy.py`  
- Bridge hard-gate: shared principle/mechanism **and** spans  
- Counterbalance v0: versioned key-set data file (not per-corpus Python), applied when Direct subjects hit misuse keys  

**Must work on `qdrant_only` with Neo4j disabled in tests.**

### Phase 3 — Universal hardening

Drain `CONCEPT_ALIASES` → lexicon; vocabulary batch/cache (SP-2/SP-3) so Fast latency budgets in `three_tier_eval` don’t blow; offline transfer_edge joins; diagnostics rationale line.

### Phase 4 — Graph-tier bonuses only

- Optional: Neo4j neighbor / relation support as *extra* Bridge/Counterbalance candidates  
- Intent-route `fact_types` (Part 6) into principle-like facts  
- Never change Fast/Hybrid store contract  

### Phase 5 — LLM on top (optional amplifier)

Structured card enrichment with span validation; grounded planner when lexicon capabilities are thin; explain seated path + caveats. **Seat set frozen before LLM.**

## 9.7 What “done” means per tier

Use the existing route harness mindset (`three_tier_eval.py` + `test_retrieval_tier_boundaries.py` + `test_hybrid_lexical_retrieval.py` store contracts).

| Assert | Fast | Hybrid | Graph |
|---|---|---|---|
| Store contract unchanged (`mongo_lexical`/`neo4j_*` flags) | ✓ | ✓ | ✓ |
| Growth query: not 8× same-shelf Direct near-duplicates when other shelves have card candidates | ✓ | ✓ | ✓ |
| Bridge seated ⇒ diagnostics include matched principle ids + evidence span ids | ✓ | ✓ | ✓ |
| Bridge **not** seated from Neo4j alone on Fast/Hybrid | ✓ | ✓ | n/a |
| Graph downgrade path: shelves still run; Neo4j bonuses absent | n/a | when downgraded | ✓ |
| `graph_reserve` and `shelf_reserve` co-exist without starving Direct | n/a | n/a | ✓ |
| Negative: pricing-like doc without shared principle ⇒ no Bridge seat | ✓ | ✓ | ✓ |

Latency targets remain those in `ROUTE_LATENCY_BUDGETS` (Fast retrieval ~2s target, Hybrid ~8s, Graph ~10s) — shelf scoring must be O(routed docs × fields), not a new Neo4j round-trip on Fast.

## 9.8 Comparison to current setup (tier lens)

| | Today | After Phases 0–3 |
|---|---|---|
| Fast | Qdrant similarity + tight MMR; thin Tier-0 | Same stores + role-reserved docs from card overlap |
| Hybrid | + lexical/hydrate/anchors; `corpus_floor` | + `shelf_reserve`; lexical helps reserved titles, not random fillers |
| Graph | + facts/Mode A/`graph_reserve` | Same, plus optional principle-link bonuses into Bridge/Counterbalance **candidates** |
| Cross-tier polymath feel | Juxtaposition when vectors agree | Curriculum-shaped packet on all tiers; Graph is richer, not required |

## 9.9 Bottom line

Deterministic-first is not a weaker librarian — it is the only librarian that **respects your tier product**. Cards and `shelf_reserve` live on the Qdrant/Mongo path every route already shares; Neo4j remains Graph Augmentation’s privilege for expansion and bonus transfer signal. LLM stays an optional layer on an architecture you can plan, test with `RetrievalTier`, and ship without waiting on generative quality.

**Build sequence reminder:** hygiene → card v0 → shelf scorer/reserve (prove on Fast) → alias/vocab hardening → Graph bonuses → LLM explain/enrich.

---

## Appendix — probe artifacts

Full JSON diagnostics for all 11 probes were saved at `/tmp/probe*.json` on the host at test time. The probe harness used: `backend/scripts_probe_query_plan.py`, `backend/scripts_probe_tier0.py`, `backend/scripts_probe_waterfall.py`, plus a temporary `tmp_probe_norerank.py` (rerank-disabled variant) executed inside the backend container via `docker exec`.

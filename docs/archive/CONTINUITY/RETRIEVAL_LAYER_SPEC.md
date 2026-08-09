# Retrieval Layer v4 — Synthesized Specification

**Date:** 2026-07-02 · **Status:** SPEC (implements the direction in RAG_PRODUCTION_REDESIGN.md)
**Provenance:** 15-agent workflow — 6 code readers mapped the ingestion architecture, 4 independent
designers (lenses: scoring-authority, tier-budgets, data-driven-signals, production-ops) produced
full designs, adversarial judges scored them (tiers 35/50 won), a completeness critic found gaps
all four missed. This document is the synthesis: the consensus core, with every judged fatal flaw
resolved and every P1 gap covered. Owner constraints: production-ready, deterministic outcomes,
increased candidate budgets, true final-packet curation, **no word lists / curated aliases as
load-bearing components**.

---

## 0. The consensus core (all four designs independently)

1. **The cross-encoder is the sole scoring authority.** Everything upstream is *rank-only*: lane
   scores (dense cosine, sparse BM25, RRF, anchor, regex) die at the lane boundary; candidates
   carry `lane_ranks`, never a comparable score. Enforced by the type system: raw lane scores are
   `DiagnosticScore`, unreadable by merge/curation. Pool-max BM25 normalization
   (`_normalize_scores_to_unit`) is deleted. This makes the measured score-fusion incident
   (Flame 0.92 > Art of Seduction 0.54) *unrepresentable*, not just patched.
2. **One listwise rerank pass per query** at the model's capacity (jina-reranker-v3, ≤64 docs per
   forward pass), fed by a deterministic prune stage with diversity quotas so multi-book/graph
   material is guaranteed to be *seen* before anything is scored.
3. **Corpus statistics replace every list.** Term admission = document frequency from a per-corpus
   `term_stats` store built with the **exact ingest sparse tokenizer** (token-exact matching:
   `\mid` ≠ `mid` by construction). `GENERIC_CONCEPT_TOKENS`, attribution lists, alias tables →
   deleted after the A/B window, not disabled.
4. **Deterministic contract.** Same `(query, config_version, stats_version, index_version)` ⇒
   byte-identical packet, asserted by `packet_hash = sha1(chunk_ids + excerpt_offsets +
   config_version)` in every trace. Total-order tie-break everywhere:
   `(score desc, doc_id asc, chunk_id asc)`.
5. **LLM components are cached, additive-only enrichment** above a deterministic floor — they can
   add fetch lanes, never remove/reorder anything.

---

## 1. Tier contracts and the candidate ladder

| Tier | Question class | Stores touched | Fetch | Prune → rerank pool | Packet | SLO (current serving) | SLO (post-oMLX) |
|---|---|---|---|---|---|---|---|
| 1 FAST `qdrant_only` | lookup / definition / single concept | Qdrant children (dense+sparse RRF); Mongo hydration only | 120 | 32 | 8 | ≤3.5s | ≤2s |
| 2 HYBRID `qdrant_mongo` (default) | analytical / multi-concept / multi-book | + Qdrant summaries, sparse lexical, doc-anchor (2.5s wall), per-side lanes | 220 | **48** (flag: 64) | 12–14 | ≤6s | ≤4s |
| 3 GRAPH `qdrant_mongo_graph` | relational / cross-domain synthesis | + Neo4j Mode A, fact seeds, decoration | 320 | **62 + 2 sentinels** (16 graph-reserved) | 14–16 + `<key_facts>` ≤8 | ≤12s | ≤8s |

- Fetch depths are config, defined relative to corpus size (`min(220, 25·log₂N)` shape) so
  486 → 5,000 docs scales without silent recall collapse (critic gap P2).
- **Latency honesty (judge flaw):** the measured rerank floor is 2.1s/24 docs on the contended
  Metal GPU; 64-doc pools are NOT free. Tier 2 defaults to 48 until serving consolidation
  (Phase 5) lands; B2 query-guided excerpts cap rerank input at ~500 chars/doc, which is the
  actual driver of listwise cost.

## 2. Pipeline stages (typed, replacing the orchestrator sprawl)

```
plan(query, conv_state, corpus_stats)        -> QueryPlan {anchors, sides, domain_prior, ladder}
fetch(plan, tier)                            -> list[LaneResult]   # rank-only outputs
prune(lanes, quotas)                         -> PruneSet           # exactly N, RRF-fused
rerank(prune_set, query)                     -> RerankedSet        # ONE listwise pass, calibrated
curate(reranked, plan, budget)               -> Packet {items, gate, packet_hash}
```

### 2.1 Planning — statistics, not lists
- Tokenize the query with the ingest sparse tokenizer (NFKC, script-aware).
- **Anchor terms:** tokens with `2 ≤ df` and `df/N ≤ 0.05` (per-corpus `term_stats`, built as the
  last ingest step with atomic snapshot replace; `stats_version` bumps per ingest batch —
  version-keyed caches, no TTLs). "common/great/mid" fail on df; no list exists to maintain.
- **Sides:** LLM decomposer (raced, deadline-capped, overlapped — shipped 037b1f0) proposes sides,
  cached by `(normalized_query, plan_version)` for replay stability; each side must carry ≥1
  df-admitted anchor **or a document-anchor match** (title-keyed sides — resolves the judge's
  "The 48 Laws of Power is all common words" flaw). Deterministic fallback: df-vector clustering
  of anchor terms. `strict_reproducibility` flag disables LLM planning entirely.
- **Follow-up turns (critic P1):** planning operates on the *rewritten* query from
  query_refinement; the rewrite is cached keyed by conversation-state hash so replays are stable.
- **Zero-admitted-terms fallback (critic P3):** dense + summary lanes ALWAYS run regardless of
  term admission — a guaranteed recall floor; term-keyed lanes are additive.
- **Domain prior:** top-12 summary-probe vote over Ghost-A `domain` (already stored on parents).
  Used only in curation tie-breaks and diagnostics — polysemy is otherwise solved by letting junk
  reach the cross-encoder and die on one scale.

### 2.2 Fetch — lanes are recall providers
Tier-2 lanes: dense children 80 · sparse children 60 · summaries 40 (RRF) · doc-anchor 20
(2.5s wall, label-table cached) · per-side 20 each. Tier 3 adds Mode A 48 + fact-evidence 16.
NOISY_KINDS stays a Qdrant `must_not` filter (ingest taxonomy, not a word list).
**Multi-corpus (critic P1):** per-corpus `term_stats`; prune quotas round-robin per corpus
(ports fair mode). **Code lane (critic P1):** `language`-tagged chunks keep the existing
cross-encoder bypass; they get a prune quota and are ordered rank-only within their section;
df stats are computed per language partition (identifier tokens are df-rare by nature).

### 2.3 Prune — diversity BEFORE scoring (deterministic)
Weighted RRF over lane ranks: `Σ w_lane/(60+rank)` (weights versioned in config). Quotas applied
at prune, not packet: per-doc ≤6 · **per-side ≥8** (pool-level side guarantee — resolves the
judges' unanimous multi-book-regression flaw) · graph 16 (Tier 3) · summaries ≤16 · code lane ≤8.
Fill to N by fused rank with the global tie-break.
**Graph without circularity (judge flaw):** Tier 3 runs hybrid prune-A (48) first; Mode A
expansion seeds from prune-A's top-8 *fused ranks* (pre-rerank, deterministic) while pool
hydration proceeds; union with the 16-slot quota → one rerank. No second scoring pass.

### 2.4 Rerank — one pass, anchored calibration
**The listwise-calibration flaw (all four judges) and its resolution:** listwise logits are
pool-relative, so absolute Platt thresholds are unsound as-is. v4 injects **two sentinel
documents** into every pool (a fixed known-irrelevant null passage and a fixed generic-relevance
passage, versioned with the calibration artifact). Calibrated score:
`p_i = σ((logit_i − logit_null − μ) / T)` — shift-invariant across pools, restoring absolute
meaning for floors and gates at the cost of 2 pool slots. The sentinels double as a per-query
health probe: if `logit_null` lands above the pool median, the reranker is sick →
`degraded=true` + alert (ends the green-while-dead class: two incidents this week).
`(μ, T, sentinel_texts)` = `calibration.v1`, fit offline on labeled pairs drawn from the golden
set + the five incident queries (label provenance, critic P2); refit triggers: model swap or
sentinel-drift alarm. Score-mutation machinery (grounding multipliers, heading/kind penalties,
degree boosts) is **deleted** — recall jobs move to prune quotas, selection jobs to curation.

### 2.5 Curation — the final-packet algorithm (owner: "true chunk curation")
Inputs: reranked candidates, plan (sides S, min_sources=2), token budget **B = 6,500** evidence
tokens (Tier 1: 3,000), packet target P.
1. **Floor:** drop `p < 0.22` or `p < 0.35·top1`; always keep best 4. Graph-role floor 0.15.
2. **Parent coalesce:** best child per parent + parent summary; `sibling_hits` recorded (packet
   ships parent-grounded text; the packet's p = max of member children — critic P3 resolved).
3. **Near-dup:** 8-token shingle containment ≥0.60 drops the lower-p (SimHash-64 payload field
   at ingest accelerates this — grafted from the signals design).
4. **Allocation (multi-book guarantees preserved from evidence_allocation):**
   per-side best-distinct-doc seats (`role=side_guarantee`, cap-exempt) → global ≥2-distinct-docs
   swap rule → p-desc fill with **per-doc cap 3** → Tier-3 ≥2 `graph_bridge` seats if any survive.
5. **Ordering:** documents by best p desc; *within* a document, corpus position order
   (parent_idx/child_idx from the ID scheme) — coherent reading for the model.
6. **Excerpts (B2 kept):** per-item budget `clamp(150, 500, B·pᵢ/Σp)` tokens; query-guided window
   keyed on df-admitted anchor positions.
7. **Budget:** shrink unprotected excerpts round-robin to the 150-token floor before dropping;
   `side_guarantee` items never dropped.
8. **Emit** `packet_hash` + per-item `ScoreLedger` (lane_ranks, p, role, curation_events:
   deduped_against / seated_by / clipped_to — grafted from ops design).

### 2.6 Gate — distributions, not token bingo
`answerable = top1_p ≥ 0.45 AND mass(p ≥ 0.30) ≥ 2`. Relationship queries keep 4e47cdb lenient
semantics score-driven: side answerable iff its `side_guarantee` seat has `p ≥ 0.30`; one strong
side ⇒ answer, absent side named. Both mirror gates (retriever sufficiency repair driver + chat
refusal arbiter) read the same calibrated fields via answerability_tuning.
**Web fusion (critic P1):** web snippets never compete for corpus evidence slots — separate
`<web_evidence>` packet section, rank-ordered by web evidence score, own provenance; the corpus
gate never reads them.

## 3. Determinism contract
- Non-degraded runs: `(query, config_version, stats_version, index_version)` ⇒ identical
  `packet_hash`. CI-tested (5-run stability on the golden set) and nightly-replayed against a
  pinned corpus snapshot.
- **Any** lane timeout/circuit-break ⇒ `degraded=true` + reason codes; degraded runs are excluded
  from the hash contract but fully traced (resolves the judges' silent-nondeterminism flaw).
  Degraded-rate is an alerting metric.
- One versioned `RetrievalConfig` (single file, content-hashed into every trace and cache key).
  Neo4j reads carry their own `graph_stamp` in the trace (freshness disclosed, critic P3).
- Deterministic degradation ladder per dependency: reranker down ⇒ RRF-over-lane-ranks packet +
  `gate=unknown` + loud alert (never a silent score-sort masquerading as rerank); embedder down ⇒
  sparse-only fetch; Neo4j down ⇒ Tier 3 serves Tier 2 + `degraded`; Mongo down ⇒ fail loud.

## 4. Serving & operations
- **Phase-5 consolidation:** one continuous-batching MLX process (oMLX/vllm-mlx) hosting embedder
  + reranker with request classes (query-embed 150ms deadline · rerank 2.5s · ingest preemptible).
  Kills the Metal-contention class. Determinism note (judge flaw): batch-shape float variance is
  accepted as sub-tie-break noise — the contract is rank/packet stability, verified by replay,
  with tie-breaks that don't hinge on float equality.
- Real-work health probes: sentinel logits per query + a 2-doc rerank probe per 60s.
- Golden set: 80 queries (the five incidents are named regression tests) with expected-source,
  no-junk, multi-doc-coverage, determinism, and latency assertions — CI merge gate.
- Concurrency: single-user deployment; one in-flight rerank, queued with backpressure + trace.

## 5. Migration phases (each A/B-gated on the golden set)

| Phase | Change | Acceptance |
|---|---|---|
| **P1 — Scoring wall** | Rank-only lanes (`DiagnosticScore` type), delete pool-max normalization + grounding/boost score edits; single rerank pass at current pool 24→32; curation reads p only | Seducer battery: 0 off-topic docs in packet; gate battery 5/5; task #12 closed by design |
| **P2 — term_stats + planning** | Per-corpus df store (ingest-final step), df-based anchors/sides, lists → deprecation shim; zero-admitted-terms floor | Replay of the 3 incident queries mints 0 junk lanes **with the lists empty**; no recall loss on golden set |
| **P3 — Ladder + prune + calibration** | Fetch 220/320, prune quotas, pool 48/62+2 sentinels, calibration.v1, distribution gate | Multi-book coverage ≥ current; retrieval SLOs hold on current serving; packet_hash stable ×5 |
| **P4 — Curation module + one lexical engine** | PacketBuilder as specified; finish sparse-BM25 backfill; retire Mongo $text/regex lanes + `_regex_score` | Packet-quality panel ≥ baseline; lexical junk class unreproducible |
| **P5 — Serving + repo shape** | oMLX consolidation; orchestrator → typed stages; golden set in CI | Tier SLO column 2; suite + golden green; watchdog probes live |

**Guardrails:** phases ship behind flags with the current path as fallback; multi-book allocation
(Sambenja flows) is a named golden-set section that every phase must pass; the operating rule
applies — hypothesis → live probe → numeric acceptance → promote.

## 6. Explicitly rejected / deferred
- Absolute-threshold calibration without sentinels (unsound for listwise — judges, unanimous).
- MMR in the ranking path (replaced by prune quotas + curation constraints — deterministic).
- Domain hard-filtering (soft prior only; the authority decides polysemy).
- Per-query LLM reranking (nondeterministic authority — violates the owner contract).
- Multi-tenant ACL caching (single-user today; cache keys must gain a principal field before any
  multi-user deployment — flagged, critic P2).

# RAG Production Redesign — From Curated Lists to Data-Driven Pipeline

**Date:** 2026-07-02
**Status:** DESIGN — approved direction, phased migration below
**Origin:** Live quality incident (seducer query) after a day of speed work.
Three junk-lane incidents in one day, each fixed by growing a stopword list
(~30 words added across `1bf77a8`, `0ab29be`, `20e9d8c`). King's verdict:
"the junk evidence is not scalable or production — it's ever-growing."
He is right. This doc is the fix for the *class*, not the instances.

---

## The five root diseases (each measured, not theorized)

### 1. Curated lists as query understanding
`concept_groups()` filters tokens through hand-maintained frozensets
(`BASE_STOP_WORDS`, `GENERIC_CONCEPT_TOKENS`, `GENERIC_CONTEXT_TERMS`,
attribution verbs, `CONCEPT_ALIASES`, `_CONCEPT_RECALL_HINTS`). Every word
not on a list becomes a concept → evidence lane → required atom → support
retrieval. Measured failures: `according` (fake lane + wasted gap-fill),
`describes` (same, caught by post-deploy probe), `common`/`great`/`mid`
(lanes for degree adjectives; `mid` lexically matched LaTeX `\mid` in a math
textbook; 5-6 off-topic docs seated in an Art of Seduction question).
A blocklist enumerating non-substantive English never converges.

### 2. No single scoring authority
~6 score spaces negotiate the final packet: dense cosine, pool-max-normalized
BM25 (`_normalize_scores_to_unit` — top lexical hit ≈0.9 BY CONSTRUCTION),
`_regex_score`, lane-match scores, `apply_query_grounding` boosts, MMR
bonuses. Measured: Flame Game Dev at 0.920453 and Template Metaprogramming
C++ at 0.884687 in the FINAL packet for a seduction query, above the real
Art of Seduction chunks (0.54) — normalized-BM25 scores survived fusion past
the cross-encoder (which would score those chunks ~0.05). Tracked as task
#12; Phase 1 below deletes the class.

### 3. The gate reasons about surface tokens
`required_evidence: concept:common, concept:great, ...` — answerability is
computed as token-coverage bingo, so junk tokens drive refusals, repair
rounds, and gap-fill retrievals. Evidence STRENGTH (cross-encoder score
distribution) is the production signal; token coverage is a proxy that
inherits every disease-1 failure.

### 4. Lane accretion
Funnel A + Funnel B + Mongo lexical + document anchor + coverage pass +
evidence-plan pass + sufficiency repair + grounding + diversity reserves —
each lane patched a recall gap; each added a score space, a latency tax
(anchor: 11.8s stall, capped `374e693`), and a junk vector. The speed
campaign (04ff365 etc.) already proved most support work is redundant when
the main pipeline is healthy: lightweight supports (children+lexical only)
lost no accuracy on the gate battery.

### 5. Manual probing as QA
Every regression this week was caught by hand-run probe batteries
(scratchpad rag_probe*.py), not by CI. The suite (1710 tests) is green while
live quality regresses — unit tests can't see retrieval quality.

---

## Target architecture

```
QUERY
  ├─ deterministic plan (instant, fallback)
  └─ LLM query analysis (raced 2x, 10s deadline, OVERLAPPED with retrieval
     — already shipped 037b1f0, effectively free)
        → 1-4 SEMANTIC sub-queries ("seduction traits", "life-stage change")
                    ↓
  ONE hybrid retrieval per sub-query:
    Qdrant dense + sparse-BM25, SERVER-SIDE RRF fusion
    (one engine, one score space; IDF handled by the index, not Python)
                    ↓
  UNION of candidates → cross-encoder LISTWISE rerank, one call
    (THE scoring authority — nothing else scores the final packet)
                    ↓
  Selection: MMR + per-doc caps over RERANK scores only
                    ↓
  Gate on SCORE DISTRIBUTION (top score, mass above threshold, margin)
                    ↓
  Packet with query-guided excerpts (B2 — keep, it's already the right kind
  of component: data-driven, no lists)
```

What dies: standalone Mongo lexical lane (after sparse backfill), anchor as
a scored funnel (demote to Qdrant metadata pre-filter or recall-only),
coverage-vs-evidence-plan as separate machineries, grounding score boosts,
GENERIC_* blocklists, per-lane lexical floors.

What lives: parent/child small-to-big, the embed micro-batcher, query-guided
excerpts, the probe harness (formalized), the trace/diagnostics plumbing.

---

## Migration phases — each A/B-gated by the golden eval set

**Phase 1 — Single scoring authority (small diff, kills task #12 by design).**
Funnels become recall-only: every candidate that can reach the final packet
goes through the cross-encoder; fused/normalized/lexical scores never rank
the final set. Lexical/anchor nominations enter the rerank pool score-less.
Acceptance: seducer-query battery shows 0 off-topic docs in final packet;
gate battery unchanged.

**Phase 2 — DF-based term admission (deletes the blocklists).**
A term anchors a lane/atom only if its corpus document frequency is under a
threshold (e.g. df/N < 0.10). Source: one Mongo aggregation per corpus,
TTL-cached like the doc-label table (or Qdrant sparse IDF). The GENERIC_*
lists become a deprecation shim; delete after an A/B window. Acceptance:
replaying the three incident queries mints zero junk lanes WITH THE LISTS
EMPTY.

**Phase 3 — Decomposer-primary planning.**
LLM sides (semantic) are the default lane source; deterministic token plan
is the fallback when the racer loses. Coverage pass + evidence-plan pass
merge into ONE sub-query retrieval concept through the same pipeline.

**Phase 4 — One lexical engine.**
Finish the sparse-BM25 backfill for legacy corpora (lexical.py docstring
already declares this intent), retire Mongo `$text`/regex lanes, and with
them `_regex_score`, `_normalize_scores_to_unit`, and the score-inflation
class entirely.

**Phase 5 — Production hardening.**
- Golden eval set in CI: ~30 queries with expected-source + no-junk +
  latency assertions (formalize scratchpad rag_probe_gate.py). Merge-gating.
- oMLX serving consolidation (task #11): one continuous-batching server for
  embedder + reranker; kills the Metal-contention and silent-degradation
  class (two incidents this week; watchdog task #9 is the interim).
- Repo shape: chat_orchestrator.py (~9k lines) decomposes into typed
  pipeline stages (plan → retrieve → rerank → select → gate → packet) so a
  scoring change is a 50-line diff.

---

## Non-goals / guardrails
- Don't rip out lanes before Phase 1+2 land — the support machinery is what
  currently rescues multi-book allocation (Sambenja flows, ea4b348 series).
- Every phase ships behind the operating rule: hypothesis → live probe →
  numeric acceptance → promote. Baselines as of this doc: Tier2 e2e ~10s,
  retrieval ~6s, gate battery 5/5, suite 1710 green.
- Known open defects riding this design: task #12 (lexical score inflation),
  task #9 (watchdog, domain-tag scoping), task #11 (oMLX).

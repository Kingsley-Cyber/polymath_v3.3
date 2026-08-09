# Bridge Retrieval & Cross-Domain Recall — Audit + Design

**Date:** 2026-07-02 · **Symptom:** high precision, low cross-domain recall — the system finds the
directly-relevant chunk but misses adjacent/analogical chunks from other books that answer the
question at the *mechanism* level. **Method:** 4-auditor code audit + live measurement of the
owner's exact synthesis query. Companion to METADATA_LAYER_AUDIT.md and RETRIEVAL_LAYER_SPEC.md.

## Live baseline (measured, not assumed)
Query: *"How do habits compound like neural networks, AI agents, and Robert Greene's mastery/power?"*
→ 7 chunks, 6 distinct books, 5 domains. **It did surface Mastery (Greene).** But:
- Only the 2 direct Atomic Habits chunks got real cross-encoder scores (0.903/0.799); **every
  cross-domain chunk was flattened to exactly 0.500** (the support-cap) — undifferentiated.
- The bridges arrived by coverage/evidence-plan passes filling *facet/topic* gaps
  ("Building AI **Applications**"), **not** by matching the shared *mechanism* (compounding /
  feedback loop) — so it missed *The Nature of Code*'s actual weight-update passage.
Diagnosis: **the bridges are accidental, topical, and unranked** — not mechanism-driven, scored,
or guaranteed.

## The 9-point bridge-metadata audit (answers)
| # | Question | Finding (file:line) |
|---|---|---|
| 1 | Who creates each field | domain/topics: Ghost A (ghost_a.py:58-120); entities/relations: Ghost B (ghost_b.py:1668-1764); facets: build_ingest_facet_profile (facets/normalizer.py); chunk_kind: section_classifier |
| 2 | Extraction method | domain/topics/summary = LLM (Ghost A); entities/relations = GLiNER/GLiREL + schema (Ghost B); facets = alias/rule (normalizer `_CONTENT_FACET_ALIASES`); chunk_kind = rule |
| 3 | In Mongo? | parent_chunks: summary, domain, topics, semantic_facets ✅; chunks: chunk_kind, domain (M1 backfill); ghost_b_extractions: entities/relations/facts |
| 4 | In Qdrant payload? | domain (M1), chunk_kind, chunk_type, language, facet_ids/text, content_facet_* ✅; **mechanisms ❌** |
| 5 | Qdrant payload index? | corpus_id, doc_id, chunk_type, chunk_kind, source_tier, user_id, language, **domain (M1)** |
| 6 | In Neo4j? | Entity (object_kind, canonical_family), Relation (30 predicates → relation_family). **Mode A cache carries fragile_bridges / structural_analogies / transfer_candidates — UNUSED** |
| 7 | Used by hybrid retrieval pre-rerank? | corpus_id + chunk_type + chunk_kind(must_not) only. domain/facets/topics/mechanisms: **NOT pre-filtered.** No domain hard-filter (correct) |
| 8 | Graph live or precomputed? | Mode A = live Cypher from seed entities, graph tier only; bridge cache precomputed at ingest but **only populated if the ingest flag ran** |
| 9 | Used in final diversity selection? | per-doc caps yes; **per-domain cap only for BROAD/global; no mechanism/bridge quota; no bridge-slot reservation** |

## Current state — three specific defects (all confirmed)
1. **No mechanism layer.** domain (18-value taxonomy) + topics (2-4 keywords) + facets (narrow
   topic slugs) + relation predicates (structural: uses/depends_on) — **nothing encodes
   transferable mechanisms** (compounding, feedback_loop, reinforcement, spaced_repetition,
   threshold_dynamics). A Greene chapter and an Atomic Habits chapter that both explain
   "repetition changes system state" share zero matchable metadata.
2. **No bridge lane.** Every lane (funnel_a/b, lexical, anchor) searches the raw query for
   *direct* relevance. HyDE rephrases; the LLM decomposer splits the query into direct sides;
   BM25 + CONCEPT_ALIASES are *intra-domain* synonyms only. Nothing searches for
   same-mechanism-different-vocabulary. No synthesis/cross-domain query type triggers bridging.
3. **No cross-domain guarantee in curation.** One book can't dominate (hard_doc_cap=4), but the
   per-domain cap (`_CHAT_COVERAGE_DOMAIN_CAP=3`) fires only for BROAD/global; synthesis queries
   (often BALANCED) get no domain-diversity floor, no reserved bridge/contrast slots, and (since
   tonight's support-cap) bridge chunks are flattened to 0.5 rather than ranked.

## What we REUSE (audit's best news — most of the plumbing exists)
- **Ghost A is one prompt field from mechanisms** (ghost_a.py:58-120): emit
  `{summary, domain, topics, mechanisms}`; parse +1 line; store on parent_chunks +1 line;
  rides existing Qdrant summary payload. **136k parents, and re-taggable from EXISTING summaries
  — no re-summarization, no re-embedding.**
- **Summary vector index already exists** (chunk_type='summary' in naive+hrag; funnel_a queries
  it) — a bridge lane queries the *section-summary* level, which is where mechanisms live.
- **Mode A bridge cache** (structural_analogies/transfer_candidates) is coded and idle — a graph
  bridge lane can light up once ingestion populates it.
- **Diversity scaffolding exists**: `_CHAT_COVERAGE_DOMAIN_CAP`, `graph_reserve`,
  `_RESERVED_SUPPORT_ROLES`, evidence_allocation per-side reservation — extend, don't build.
- **Deterministic intent** (QueryNeed) is the hook for a synthesis/bridge signal.

## Build plan (phased, each flag-gated + acceptance-tested on the habits-NN query)

**B1 — Mechanisms metadata (the enabler).** Add `mechanisms: list[str]` to Ghost A output
(open extraction, 3-6 transferable-mechanism phrases per parent). Forward path: prompt + parse +
store + Qdrant summary payload + keyword index. Backfill: `scripts/backfill_mechanisms.py` reads
each existing parent **summary** (already written) and emits mechanisms — LLM pass over 136k
parents, resumable, deepseek-chat (cheap model, per DeepSeek gotcha). *This is a background job
(hours), kicked off by the owner; the forward path + script ship now.*
Acceptance: parents carry 3-6 mechanisms; Mastery + Atomic Habits + Nature of Code share
≥1 mechanism (compounding / reinforcement).

**B2 — Synthesis query type + bridge lane.** Extend intent to detect SYNTHESIS/cross-domain
(deterministic: "how does X relate to/like/compare Y", multiple named entities across domains).
For synthesis queries add a **bridge lane**: map query→mechanisms (embed query vs the mechanism
vocabulary, or extract via the same tags), query the **summary index** boosted by shared
mechanisms across *distinct* domains/books. Domain stays a SOFT boost, never a gate.
Acceptance: bridge lane surfaces Nature of Code's weight-update passage for the habits-NN query.

**B3 — Diversity/coverage quotas (testable NOW, no backfill).** For synthesis intent:
per-domain cap applies (not just global); reserve K slots for bridge evidence from a domain
distinct from the direct-answer domain; require ≥3 distinct books; **score bridge candidates by
the cross-encoder** (fold them into the single rerank pool) instead of flattening to 0.5.
Acceptance: habits-NN packet = ≥3 domains, ≥4 books, bridge chunks CE-scored not 0.5, direct
Atomic Habits still on top.

**B4 — Light Mode-A bridge lane (reuse).** Populate the Mode A bridge cache at ingest; wire the
graph bridge lane for synthesis queries at the graph tier (structural_analogies/transfer_candidates).

## Verification harness (the owner's requested test)
`scratchpad/bridge_probe.py`: run the habits-NN query, print candidates **per lane** (dense /
BM25 / bridge / summary / graph) **before and after rerank**, and report source diversity,
domain diversity, distinct-book count, direct vs bridge score bands, and which useful chunk (if
any) was filtered out. Ships with B3.

## Guardrails
- Domain is a SOFT boost / diversity signal — NEVER a hard pre-filter (would fence off synthesis).
- Mechanisms feed *recall* (bridge lane) and *diversity* (quota) — the cross-encoder remains the
  sole ranking authority (v4 scoring wall); mechanisms never become a score multiplier.
- Every phase A/B-gated on the habits-NN probe + the existing gate battery (no precision loss).

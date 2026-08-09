# Retrieval pipeline — end-to-end wiring verification (2026-06-14)

18-agent adversarial audit (trace → independent re-verify) of every retrieval change this
session, producer → carrier → consumer → prompt. One critical wiring gap was found and fixed.

## Per-feature wiring checklist

| Feature | Verdict | Producer → Carrier → Consumer (file:line) |
|---|---|---|
| **SourceChunk.domain** | ✅ FULLY_WIRED | parent_chunks.domain (backfill) → hydrate_chunks `chunk.domain=pc["domain"]` (hydrate.py:185) → FacetCandidate.domain (chat_orchestrator.py:1895) → select_facet_final can_add (final_selector.py:114) |
| **per-domain cap** | ✅ FIXED (was PARTIAL) | `_CHAT_COVERAGE_DOMAIN_CAP=3` → gated `search_mode=="global"` (chat_orchestrator.py:2251) → select_facet_final max_per_domain. **Fix:** global/summary hydration now sets domain (hydrate.py:442) — previously `hydrate_summary_rerank_texts` dropped it, making the cap a no-op in the only mode it fires in. |
| **source_cap (live setting)** | ✅ FULLY_WIRED | RetrievalSettings.source_cap (schemas.py:465) → _resolve_query_profile (chat_orchestrator.py:4691) → _enforce_chat_query_coverage `max_sources=max(final_top_k,source_cap)` (2241) → select_facet_final hard cap (final_selector.py:106) |
| **reranker score cap** | ✅ FULLY_WIRED | reranker.py:97 chunk.score → bounded_score=min(max(score,0),0.8)*2 in final_score (chat_orchestrator.py:1674) — raw score never enters final_score directly |
| **pre/post-rerank breadth log** | ✅ FULLY_WIRED | counts["distinct_docs_merged"] (retriever/__init__.py:1021) + _pool_doc_ids (1194) → retrieval_pool_breadth logger.info (1206). Diagnostic-only. NOTE: `premerge` label is actually post-merge (value is real). |
| **weak-chunk legend** | ✅ FULLY_WIRED | strength="weak" (chat_orchestrator.py:1751) → support_strength metadata → inline `[strength=weak]` tag (context_manager.py:490) → `<evidence_policy>` legend prepended when present (627-636) → reaches LLM via stream_chat (3900) |
| **lexical-floor fix** | ✅ FULLY_WIRED | (A) metadata_facet_terms folded into summary_text (chat_orchestrator.py:1452); semantic_facets produced at ingest (normalizer.py:730 → worker.py:1128 → hydrate metadata). (B) escape hatch bumps facet_score to threshold when facet.semantic_matched (1666); matching_vector_facets sets it (runtime.py:440). |
| **topics field** | 🪦 DEAD (intentional/future) | Produced (backfill_parent_domains_llm.py:124 → parent_chunks.topics) but NO carrier: SourceChunk has no `topics` field, hydrate never reads it, no consumer. Stored for future faceting only. |
| **model fallback (litellm)** | 🪦 DEAD for streaming | router_settings.default_fallbacks=["deepseek-chat-fallback"] (config.yaml) is NOT applied by litellm v1.60.0 on a 500 raised mid-stream (the live chat path is always stream=True); backend has no in-process retry. |

## Critical fix this session
**domain_cap was self-defeating.** Gated on `search_mode=="global"`, but global mode hydrates via
`hydrate_summary_rerank_texts`, which set text/summary/heading/chunk_kind/metadata but **not** `domain`.
So global-mode chunks reached `select_facet_final` with `domain=None`; `can_add`'s `... and domain`
short-circuited → cap was a no-op in the exact mode it gates on. **Fixed** by adding `domain` to the
summary-hydration projection + assignment (hydrate.py). Verified: a summary-mode SourceChunk now
hydrates `domain="machine_learning"` from parent_chunks.

## Broad-query integration test — "What are the main themes across my corpus?"
1. **Intent** → `resolve_search_mode` returns `global` ("main themes"/"what are the main" are markers). ✅
2. **Retrieval** → Funnel-A summaries; breadth log emits distinct_docs_premerge/postrerank. ✅
3. **Rerank** → scores assigned; breadth log shows if rerank collapsed document spread. ✅
4. **Hydration** → `hydrate_summary_rerank_texts` now attaches `domain` (FIXED) + summary text. `SourceChunk.domain` populated. ✅
5. **Selection (`select_facet_final`)**: source_cap caps distinct docs (hard); `max_per_domain=3` now fires (domain present) → spreads ≤3 chunks/domain across disciplines; reranker score bounded so it can't dominate; semantic lanes promoted via escape hatch. ✅
6. **Prompt (`build_augmented_prompt`)**: context_block spans multiple domains/documents; `<evidence_policy>` weak legend prepended when weak chunks present; reaches the LLM via message_dicts → stream_chat. ✅

**Result:** the final context sent to the LLM now spans multiple domains and documents, with
per-chunk confidence labels (`[strength=weak]`) and the decoding legend — the rule-6 success
criteria. Theme breadth is no longer capped at the reranker's favorite discipline.

## Remaining (precise next steps)
- **model fallback (streaming):** add an in-process retry in the two synthesis stream guards
  (chat_orchestrator.py:3934 tool-loop, 4455 final no-tool): on a stream failure with empty
  accumulated content, re-call `stream_chat(model="deepseek/deepseek-chat", ...)` *without*
  per-request creds (routes via litellm deepseek/* + env key). Bounded-risk (retry-failure falls
  through to the existing error emit). **Operational mitigation already live:** the stale
  DEEPSEEK_API_KEY was fixed, so `deepseek/deepseek-chat` works through litellm — switching the
  chat model to it resolves blank answers immediately.
- **topics:** wire into prompt assembly (a per-chunk topics line, or a corpus topics overview) if a
  consumer is desired; otherwise it remains intentional future data.
- **breadth log label:** rename `distinct_docs_premerge` → `distinct_docs_postmerge` for accuracy.
- **domain ingestion:** Ghost A emits plain text today; emitting `{domain, topics}` at summary time
  (vs the offline backfill) would keep new ingests domain-tagged without a re-backfill.

# Polymath RAG — Speed & Quality Roadmap (living tracker)

> Single source of truth for the retrieval optimization work. Check items off as we ship them.
> Branch: `codex/evidence-plan-retrieval`. Last updated: 2026-06-29.

**Status legend:** `[ ]` todo · `[~]` in progress · `[x]` done · `[—]` skipped/won't-do (verified)

---

## TL;DR — the reframe
Your latency is **NOT vector search** (that's sub-second). It is:
1. **LLM calls** on the critical path (HyDE ~4s).
2. **No RAG caching** — the same embed/search work is redone every time.
3. **Per-call connection overhead** — new sockets to the MLX sidecars 6–9×/query.

Two separate tracks:
- **Track A — SPEED** (cut the ~15s).
- **Track B — QUALITY** (NotebookLM-style grounding; the hydration precision gap).
- `#1 hydration` pays **both** (4× smaller prompt = faster *and* better grounded).

## Measured baseline (live, 2026-06-29)
- ~15s/query: HyDE ~4s + RAG/coverage ~11s; pure vector search < 1s.
- LLM evidence-plan decomposer: **OFF** (parked by user; reliable when on but slow w/ minimax).

## How we work (one rule)
One item at a time → **build → asserting test (non-zero exit on fail) → before/after on a live query → commit on branch.** No blind flips; behavior changes go behind a flag + A/B.

---

## TRACK A — SPEED

### Tier 1 — biggest, safest wins (do first)
| ✓ | # | Change | Where | Speed | Effort | Q-risk |
|---|---|--------|-------|-------|--------|--------|
| [x] | A1 | **Pooled httpx to sidecars** (embedder module-level, reranker instance-scoped) + **Mongo `minPoolSize/maxPoolSize`** + **`_embedding_config_for_query` TTL cache** + **Qdrant gRPC gated** (`QDRANT_PREFER_GRPC`, default off, validated reachable) | `embedder.py`, `reranker.py`, `conversation.py:49`, `retriever/__init__.py`, `config.py`, funnel_a/b + lexical | per-call overhead gone | S | none | **DONE — commit `1fc3d84`. 80 tests green; live retrieval intact (9 sources), warm ~5s.** Next: flip `QDRANT_PREFER_GRPC=true` when ready (validated). |
| [x] | A2 | **Retrieval-result cache** (`cache_util.TTLCache`, 120s) wrapping `retrieve()`→`_retrieve_uncached()`; key=(query,corpus,tier,mode,knobs); deep-copy on store/return (mutation-safe); positional calls bypass | `cache_util.py`, `retriever/__init__.py`, `conftest.py` | instant repeats | M | low | **DONE — commit `342dccb`. Live: repeated specific query 0.3s (cache hit). 321 tests green.** |
| [x] | A3 | **Fast-path**: `intent=specific` + <2 evidence lanes + not global → skip facet-coverage **and** evidence-plan extra retrievals | `chat_orchestrator.py` stream path | big on simple Qs | M | low | **DONE — commit `e082eba`. Live: specific ~2.0s vs multi ~4.6s.** |
| [—] | A0 | ~~HyDE result cache + fast-model default + parallel-race~~ | — | ~3–4s | S/M | **PARKED by user** |

### Tier 2 — next layer
| ✓ | # | Change | Where | Speed | Effort | Q-risk |
|---|---|--------|-------|-------|--------|--------|
| [~] | A4 | **Qdrant int8 scalar quantization** + `hnsw_ef` | `qdrant_writer.py:449,471` + funnel search params | 2–3× search | M | low | **DEFERRED — low ROI for *latency*: vector search is already sub-second; the bottleneck is LLM calls + hydration, not ANN. Main benefit is memory/scale (4× smaller vectors) + needs a one-off migration of 22 live collections. Do as a separate memory/scale effort, not a latency win.** |
| [~] | A5 | **Batch lane embeds + `query_batch_points` + concurrent coverage/evidence** | `chat_orchestrator.py`, `retriever/__init__.py` | collapses N round-trips | M | none | **DEFERRED — coverage→evidence are DATA-dependent (evidence consumes coverage output), so not parallelizable; and repeated lane embeds are now mostly absorbed by A6 (embedding cache) + A2 (retrieval cache). Marginal remaining gain for a medium refactor.** |
| [x] | A6 | **Query-embedding cache** (`cache_util.TTLCache`, key=model+dim+text) wrapping `embed_query` | `embedder.py:430`, `cache_util.py` | dedupe intra-turn | S | none | **DONE (query-embedding half) — commit `e082eba`.** Reranker-score cache (A6b) deferred: limited (query,chunk) overlap across lanes; low ROI. |
| [—] | A7 | **Raise `RERANKER_HTTP_BATCH_SIZE`** | `reranker.py:39` | fewer rerank round-trips | S | low | **WON'T — the llama.cpp Qwen3 sidecar hard-fails ~512-token physical batch; 8 is deliberately tuned to it. Raising it triggers split-retries (slower). Tunable env knob if you move to a larger-batch sidecar.** |

### Tier 3 — architectural (future bets)
| ✓ | # | Change | Impact |
|---|---|--------|--------|
| [ ] | A8 | **ColBERT / late-interaction** — cross-encoder quality at bi-encoder speed; could retire the reranker round-trip. Highest ceiling. | +quality, L |
| [ ] | A9 | **SPLADE learned sparse** — upgrade *over* current IDF sparse (not a replacement for Mongo; you already fuse dense+sparse RRF) | +quality, L |
| [ ] | A10 | **Semantic chunking** (topic-boundary) at ingest — embeds cleaner → fewer candidates needed | +quality, L |
| [ ] | A11 | **Semantic (near-dup query) cache** — GPTCache-style; do *after* deterministic caches | high speed, L, **q-risk medium** |

---

## TRACK B — QUALITY (grounding / hydration precision)

> The "NotebookLM gap." Verified against `hydrate.py` + `context_manager.py`. **B1 is the single biggest answer-quality lever and also shrinks the prompt 4×.**

| ✓ | # | Finding (VERIFIED) | Evidence | Fix |
|---|---|--------------------|----------|-----|
| [ ] | B1 | **Hydration overwrites the precise child with the full parent body**, and the summary is **never used in the prompt** (worse than first thought). | `hydrate.py:2` docstring; `hydrate.py:166` `chunk.text = pc["text"]`; prompt uses only `s.text` at `context_manager.py:772`; `.summary` unreferenced in prompt path | Make hydration a **mode** (`parent` \| `child+summary` \| `child+query-excerpt`). Capture `child_text` **before** the line-166 overwrite (it's destroyed otherwise). Default → `child + section summary` (~300 tok vs ~1200). **Behind a flag + A/B** — small-to-big is a legit pattern, some Qs want parent context. |
| [ ] | B2 | **Query-guided parent excerpt** (the smarter B1): instead of the generic summary, attach the 2–3 parent sentences most similar to the query. | same plumbing as B1; summaries are query-blind (Ghost A, ingest-time) | pick top parent sentences by query similarity at retrieval time; no re-summarize |
| [ ] | B3 | **No per-chunk "does this text answer?" gate** — answerability is concept/coverage-level only, never checks chunk *text*. | `chat_orchestrator.py:777` `_build_retrieval_answerability_gate` (heuristic, concept-level) | lightweight extractive-QA pass per chunk → drop topically-related-but-non-answering chunks; trigger support retrieval if final set has no answer-bearing chunk |
| [ ] | B4 | **No answer-bearingness reranking** — final order = reranker topical score, not "how well does this answer the question." | final order = rerank score | reuse B3's QA pass to **reorder** evidence (best answers first). *(B3+B4 = one QA pass, two uses — build once.)* |
| [ ] | B5 | **Query-relevant window clip inside child** — 128-tok window is fixed at ingest; relevant sentence may be at pos 40–80. | child window fixed at ingest | post-rerank, clip each child to its highest-scoring ~80-tok window. *(Do last — small once B1 ships the tight child.)* |

**B1 SHIPPED** — commit `15b61ff`. `HYDRATION_MODE` flag (`config.py`): `parent` (default, unchanged) | `child_summary` (precise child + section summary, ~4× denser). Pure `_assemble_hydrated_text()` + 5 unit tests. **To A/B: set `HYDRATION_MODE=child_summary` and rebuild, compare answer grounding + prompt size.** B2–B5 still open.

**Recommended B order:** ~~B1~~ → B2 → (B3+B4 together) → B5.

---

## NotebookLM-style RAG — component checklist
> Validated 2026-06-29 against code. **You already have the architecture.** Only 2 deltas, both **OPTIONAL** (tracked below — never blocking).

| Component | Have it? | Evidence |
|-----------|----------|----------|
| Semantic embedding | ✅ yes | Qwen3-Embedding via MLX sidecar (`embedder.py`) |
| Keyword / inverted index | ✅ yes | Qdrant native sparse (server-side IDF/BM25) + Mongo `$text` fallback (`qdrant_writer.py:445`) |
| Fixed top-K retrieval | ✅ yes | profile presets: `retrieval_k` 10/40/60, `final_top_k` 8 |
| Sparse keyword retrieval | ✅ yes | `SparseVectorParams(Modifier.IDF)` (`qdrant_writer.py:453`) |
| Two-stage reranking | ✅ yes | broad recall → cross-encoder rerank (`reranker.py`) |
| Broad top-K strategy | ✅ yes | oversample ~40–56 → rerank to 8 (`chat_orchestrator.py:3716`) |
| Cross-encoder reranker | ✅ yes | Qwen3-Reranker-0.6B (`config.py:430`) |
| Fine-tuned cross-encoder | ❌ no | stock `qwen3-reranker-0.6b-q8_0`, not domain-tuned → **OPT1** |
| Domain classifier (ingest) | ✅ yes | Ghost A classifies passages into `_DOMAIN_TAXONOMY` (`ghost_a.py:50,62`) |
| Domain classifier (query-time routing) | ⚠️ partial/unwired | tags exist, query isn't routed by domain → **OPT2** |

### OPTIONAL upgrades (nice-to-have; never blocking)
| ✓ | # | Change | Why it's optional | Effort |
|---|---|--------|-------------------|--------|
| [ ] | OPT1 | **Fine-tune the cross-encoder reranker** on your own (query, relevant-chunk) pairs | stock Qwen3-Reranker already works; needs labeled data; usually marginal gain. (MLX fine-tune skill makes it feasible later.) | L |
| [ ] | OPT2 | **Query-time domain routing** — use the ingest domain tags to pre-filter/boost candidates | you already tag domains at ingest; **also appears as the Track A speed item "domain self-query pre-filter"** — so it pays speed+precision if you ever do it | M |

---

## TRACK C — Ingestion-side retrieval quality (chunking · noise · metadata)
> Root cause: chunks are sized for **Ghost B's GLiNER/GLiREL NER** (128-tok windows), not for retrieval. The fix is NOT a rewrite — add a second splitting layer and *use the metadata you already build*. **Note:** chunking changes (C1/C2) require **re-ingesting** corpora (re-chunk + re-embed); C3 metadata-at-retrieval changes do **not** (data already stored). `✓` = verified in code 2026-06-29, `◦` = from analysis (verify before building).

### C1 — Chunk granularity (tuned for NER, not answers)
| ✓ | # | Issue | Status | Fix |
|---|---|-------|--------|-----|
| [ ] | C1a | No semantic boundary detection — splits on paragraph/sentence (syntactic), not topic shift | ◦ | embed consecutive sentences, split on cosine drop |
| [ ] | C1b | Fixed 128-tok children regardless of density → dense facts padded (dilution), narratives cut mid-thought | ✓ (NER band, `tier_chunker.py:46-50,69`) | variable 40–300 tok by coherence |
| [ ] | C1c | **`sentence_merge` hardcoded; semantic splitter is a STUB — the `child_chunk_algorithm` knob is ignored** | ✓ CONFIRMED `tier_chunker.py:135,161,797` | implement the splitter, OR remove the dead knob from ingestion UI/UX |
| [ ] | C1d | No proposition decomposition — 1 child = 2–4 claims → diluted embedding | ◦ | split child into 1-claim propositions as the *retrieval* unit |
| [ ] | C1e | Blind 200-tok overlap — copies noise across clean section breaks | ◦ | boundary-aware overlap (only when split is mid-thought) |
| [ ] | C1f | No self-containment check — "as discussed above…" children unusable alone | ◦ | pronoun/antecedent resolution; prepend referents |

**C1 PARTLY SHIPPED — commit `0b7e795` (deployed).** Real `semantic_split` strategy: **one child per paragraph/idea** (`_split_by_paragraph_idea`), variable size, scoped to plain body prose (tables/code/transcripts keep their splitters), with a 24-tok fragment-floor so ideas aren't re-packed. **Default for NEW corpora** (schema default flipped); old corpora keep frozen config → grandfathered, re-ingest to upgrade. Done: **C1a** (paragraph/idea boundaries), **C1b** (variable size), **C1c** (knob real + default), **C1e** (boundary-aware). **VALIDATED LIVE** — ingested a fresh fictional corpus (`chunker_qa_zorblax`, corpus `1ce26fe0…`): 5 fact-paragraphs → **5 clean single-fact chunks**, each fact query retrieved the **exact** right chunk. **B3 answerability fix shipped alongside** (commit `00f1990`): the gate scored facet/concept coverage and refused even when the chunk TEXT answered — added text-coverage fallback + dropped generic question-words. End-to-end: **0/4 → 3/4** fact queries answer correctly (4th = tiny-corpus ranking miss). 284 chunker + 53 gate/semantics tests green.
**Still open:** **C1d** full 1-claim proposition decomposition + **C1f** self-containment → the strict **two-tier** (128-tok Ghost-B unit separate from finer retrieval unit). Current version is single-tier (paragraph-aligned children used for both retrieval and NER), which keeps Ghost B windows reasonable. (Supersedes Track A `A10`.)
**UI/UX:** remove the child-chunk-size/algorithm config from the ingestion UI — it's a no-op hint today (C1c).

### C2 — Noise removal is heading-level, not content-level
✓ Section classifier drops structural noise (TOC/biblio/index/appendix) — good, those never embed. ◦ But **body** chunks pass untouched: transitional filler ("as discussed in the previous chapter"), meta-narration ("the author argues"), repetitive restatements, non-answering examples, dangling figure refs ("as shown in Figure 3.2" without the figure). All dilute embeddings.
**Fix:** content-level noise scoring at ingest (filler/meta-narration detector), or down-weight at retrieval.

### C3 — Metadata: rich at ingestion, unused at retrieval (the core gap)
**One sentence:** you build rich metadata (facets, schema-lens, deterministic facts, heading paths, chunk kinds) but retrieval uses only vector similarity + **3 boolean filters** (`corpus_id`, `chunk_type`, `chunk_kind`) — metadata only **excludes** chunks, never helps **decide** which to retrieve.
- ✓ **Facets are dead weight in Qdrant** — `facet_ids/facet_text/content_facet_ids/doc_facet_ids` stored on every point (`qdrant_writer.py:618-624`) but **not indexed, not filtered** (only used at the Mongo/orchestrator coverage layer). → either index+use them in retrieval, or stop copying them to Qdrant (write/storage cost).
- ~ **heading_path feeds the rerank text** (`ranking_policy.py:293,395`) but there's **no structural section-importance boost** (Core Mechanisms > Sidebar/Historical Note).
- ◦ **Missing per-chunk quality signals at ingest:** `self_containment_score`, `information_density`, `proposition_count`, `answer_type_hints` (definition/comparison/procedure/example/narrative), `embedding_quality_score` → would enable retrieval-time quality filtering/boosting.
- ~ Mongo lexical = basic BM25 (`$text`, heading_path 5×) + regex fallback; no stemming/synonyms/phrase-awareness. (Primary lexical is already Qdrant native sparse IDF; Mongo is the fallback path.)

**C3 fix direction (no re-ingest needed):** use the metadata already stored — (a) answer-type / heading-section boost in `ranking_policy`; (b) decide facets' fate in Qdrant; (c) add the quality signals at ingest for future retrieval scoring. Same theme as Track A's domain self-query (**OPT2**). **This is the highest-leverage, lowest-cost slice of Track C.**

### C — what's genuinely good (don't touch)
near-dup detection (5-gram Jaccard + containment), markup scrubbing pre-chunk, Ghost B skip policy (TOC/biblio/code skip LLM extraction), deterministic fact extraction → Neo4j `<key_facts>`, schema-lens facets (object_kind/domain_type/canonical_family). All confirmed solid.

---

## VERIFIED ALREADY-DONE — do NOT rebuild
| ✓ | Thing | Evidence |
|---|-------|----------|
| [x] | Qdrant **payload indexes** (incl. `corpus_id`, doc_id, chunk_id, parent_id, chunk_type, chunk_kind, language…) | `qdrant_writer.py:217,233`, `:307` create_payload_index |
| [x] | Neo4j **indexes** — entity name/type + fulltext `entity_name_ft`, doc/chunk constraints | `graph/schema.py` (37 indexes), `graph_query.py:312` |
| [x] | **Native dense+sparse RRF fusion** (you're past "Mongo BM25 only") | `funnel_a.py:148` FusionQuery RRF; `qdrant_writer.py:453` sparse `Modifier.IDF` |
| [x] | **Parent-child chunks + summaries**, doc **dedup** | tier_chunker; dedup.py |
| [x] | **Heuristic answerability gate** (NOT an LLM call — no hidden cost) | `chat_orchestrator.py:777` |
| [x] | **tiktoken + chat-model prewarm**; reranker-bypass toggle | `main.py:111,335`; `test_reranker_bypass.py` |

## DISMISSED PROPOSALS — verified they WON'T help (don't add)
| Proposal | Verdict | Why (verified) |
|----------|---------|----------------|
| `CREATE INDEX … CALLS … ON (c.corpus_ids)` | [—] won't be used | Query traverses from seed_e (`mode_a.py:500`) — not an index anchor — and filters `cid IN c.corpus_ids` (list-membership, no array index in Neo4j). Same trap as `get_full_corpus_graph`. Cost = fan-out, not index. **Also: PROFILE the existing `RELATES_TO.corpus_ids` index — likely dead weight too.** |
| `CREATE INDEX … Chunk ON (corpus_id, chunk_id)` | [—] redundant | `chunk_id` already UNIQUE (`schema.py:18`) → composite adds nothing; `expanded` is traversal-reached, so corpus_id is a post-filter not a seek. |
| `CREATE INDEX … Entity ON (graphify_community)` | [△] real but **ingestion-only** | `neo4j_writer.py:2151` equality seek, currently full label scan — but speeds **ingestion**, not retrieval. Add only if large-graph ingest hurts. |
| HyDE optimizations (cache/fast-model/race) | [—] parked | User stopped expanding the concept. |

**Real graph-latency lever (not an index):** rewrite `get_full_corpus_graph`'s `EXISTS` subquery to match the cleaner multi-corpus variant. Fan-out cost already mitigated by timeout + `RETRIEVAL_CACHE_GRAPH_METRICS`.

---

## Shipped this session (context)
- **Source-aware per-side evidence allocation** (the original 4/5-from-one-book fix) — commits `ea4b348`, `8755976`, `9f8cfd8`. Deployed live. 15 asserting tests green. See `project_polymath_source_allocation` memory.

## Open decisions
- **B1 hydration:** ship as a flag, A/B with the retrieval-eval harness before defaulting.
- **HyDE:** parked.
- **Next up:** A1 (connection pooling) — smallest/safest speed win, no logic change.

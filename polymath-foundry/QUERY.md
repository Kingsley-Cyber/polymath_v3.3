# Query — Polymath on Foundry

The full retrieval-and-answer pipeline that runs every chat turn. Each stage is a single Foundry Function exposed as an AIP Agent tool. The agent orchestrates the order based on its system prompt.

> **TL;DR pipeline**
> `(optional) expand_query → query_embed → hybrid_search → rerank → compose_answer`

---

## 1. Pipeline overview

```
User query
    │
    ▼
1. (optional) expand_query     HyDE — short/vague queries only
    │
    ▼
2. query_embed                  1024-dim vector, same space as Chunks
    │
    ▼
3. hybrid_search                Vector ANN + lexical, fused by RRF
    │                           Filters by corpora_scope
    │
    ▼
4. rerank                       Cross-encoder over top-K candidates
    │
    ▼
5. compose_answer               LLM composes answer using top-N reranked chunks
    │                           Parses citation markers
    │
    ▼
Action: CreateMessageWithCitations
    │
    ▼
Ontology: Message + Citation objects
```

---

## 2. Stage detail

### 2.1 `expand_query` (optional)

**When the agent calls it:** the user's query is short (< 8 tokens) or vague ("tell me about X").

**What it does:** asks an AIP-hosted LLM to generate one or two pseudo-answers (HyDE — Hypothetical Document Embeddings). Returns `[original_query, pseudo_1, pseudo_2]`.

**Downstream consequence:** the agent runs the rest of the pipeline once per expanded query, then fuses results at `rerank` time using reciprocal-rank-fusion across the queries.

**Defaults:** off. The agent's system prompt decides per turn based on query length and vagueness signals.

---

### 2.2 `query_embed`

**What it does:** runs the same embedding model used by `embed_chunks` on the (possibly expanded) query. Returns a 1024-dim vector.

**Why it's separate from `hybrid_search`:** so the agent can cache the embedding across HyDE variants if needed, and so it's individually testable in AIP Evals.

**Determinism:** model pinned, temperature N/A. Same input → same output.

---

### 2.3 `hybrid_search`

The retrieval workhorse.

**Two retrievers, fused.**

| Retriever | Source | Behavior |
|---|---|---|
| Vector ANN | Foundry Vector Search Service, index `polymath_chunks` | Top-K nearest neighbors by cosine similarity over `Chunk.embedding`. Filtered by `corpus_id IN (corpora_scope)`. |
| Lexical | Foundry text index over `Chunk.text` | BM25-equivalent. Same filter. |

**Fusion:** Reciprocal-Rank-Fusion with `k=60` (literature default):

```
score(chunk) = Σ (1 / (60 + rank_i))   over each retriever i where the chunk appears
```

Returns the top-K fused candidates (default K=40), in fused-score order, as `Chunk` objects.

**Why both retrievers, why fused.**
- Pure vector misses exact-match keyword queries ("FY26", "AR 25-50").
- Pure lexical misses paraphrases.
- RRF is robust to score-scale differences and needs no normalization.

---

### 2.4 `rerank`

**Cross-encoder** model, AIP-hosted. Parity with v3.3's `ms-marco-MiniLM-L6-v2`.

**Input:** `(query, chunk.text[:2048])` pairs, batched.
**Output:** the same Chunk list, reordered by rerank score descending, truncated to top-N (default N=12).

Each returned Chunk gets a `rerank_score` field attached.

**Why cross-encoder, not just ANN.**
- ANN is fast and recall-oriented. It returns plausibly-relevant chunks.
- Cross-encoder reads the query and chunk *together*; it's precision-oriented but ~100× more expensive per pair.
- The pipeline pays the expensive precision cost only on K=40 candidates, not the whole corpus.

---

### 2.5 `compose_answer`

The terminal stage. Takes the top-N reranked chunks and:

1. Builds a context block: `[1] <chunk_1_text>\n\n[2] <chunk_2_text>\n\n…`
2. Sends an AIP LLM call: "answer the question using ONLY the numbered context. Use [N] citation markers."
3. Parses the resulting text to extract citation markers and their spans.
4. Calls the `CreateMessageWithCitations` Action to atomically write the Message + Citation objects.
5. Returns the new `message_id`.

**If the LLM produces zero citation markers**, the function rejects the answer and returns an empty `message_id`. The agent reads that and decides: ask a clarifying question, or retry with a different prompt variant.

**Determinism:** temperature 0 by default. The agent can request higher temperature for "creative" turns, but the eval set runs at 0.

---

## 3. Multi-corpus scoping

`hybrid_search` accepts `corpus_ids: list[str]`. The agent reads `Conversation.corpora_scope` and passes it. If the user explicitly names a corpus mid-conversation ("just search the Doctrine corpus"), the agent narrows the call.

This is enforced at the **tool-call boundary** — the Function won't bypass corpus filters even if a buggy agent prompt tries to. The filter is part of the function signature.

---

## 4. What never reaches the LLM

By design:

- **Raw Ontology objects** — only the reranked chunk texts.
- **Embeddings** — never serialized into a prompt.
- **Other users' Conversations or Messages** — outside `corpora_scope`, blocked by Foundry ACLs.
- **Source URIs that the operator hasn't been granted on** — the Corpus security boundary makes this automatic.

---

## 5. Telemetry recorded per turn

Stored on the `Message` object (assistant turns only):

| Field | Source |
|---|---|
| `latency_ms` | wall clock of `compose_answer` |
| `token_count` | LLM-reported |
| `tools_used` | array of function names called this turn |
| Linked: `cites` → Citation → Chunk | provenance graph |

Stored on the eventual `EvalRun` (when sampled):

| Field | Source |
|---|---|
| `retrieval_recall_at_10` | held-out judge labels |
| `faithfulness` | judge model |
| `citation_coverage` | computed from Citation links |

---

## 6. Cost surface

Per chat turn, in approximate order:

| Stage | Cost driver | Typical magnitude |
|---|---|---|
| `expand_query` | 2 LLM calls @ ≤ 180 tokens out | Only on short/vague queries |
| `query_embed` | 1 embedding call | Negligible |
| `hybrid_search` | 2 index queries | Negligible |
| `rerank` | 1 cross-encoder call (batched, K pairs) | Modest |
| `compose_answer` | 1 LLM call @ ≤ 900 tokens out | Largest cost |
| Action `CreateMessageWithCitations` | 1 transactional write | Negligible |

The LLM-heavy stages are `expand_query` (optional) and `compose_answer`. Everything else is index lookups or a small cross-encoder.

---

## 7. Intent + Reasoning

- **Why each stage is its own Function.** Independent evals, independent failure isolation, independent caching. A regression in `rerank` shouldn't require touching `hybrid_search`.
- **Why HyDE is optional, not always-on.** It costs an LLM call per query; on long, specific queries it's net-negative (the expansion can add noise). The agent decides based on query length and vagueness signals.
- **Why RRF and not weighted sum.** RRF doesn't require score normalization across retrievers; the underlying score scales (cosine vs BM25) are incomparable.
- **Why the LLM never sees raw objects.** Two reasons: cost (objects are heavier than chunk text) and safety (objects can carry PII or access metadata that should not leak into a generation).
- **Why `compose_answer` rejects zero-citation outputs.** Citation coverage is a hard requirement. The agent retrying is cheaper than letting an uncited answer ship.
- **Why we write Citations through an Action, not direct dataset upserts.** Audit, validation, atomicity. Every link from a Message back to its evidence is governed the same way.
- **Why no LLM client lives in this folder.** AIP exposes models as a tool. The agent calls them; our Functions never call out to OpenRouter or any external provider.
- **Why corpus scoping is on the Function signature, not the agent prompt.** Defense in depth — even a compromised or buggy agent prompt cannot widen the retrieval beyond what the user is allowed to see.

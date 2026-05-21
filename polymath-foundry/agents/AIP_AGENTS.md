# AIP Agents

Three agents. Each is configured in AIP Chatbot Studio (formerly AIP Agent Studio). Code references live in `functions/*.md` — agents reference Foundry Functions as tools.

System prompts are kept tight (under 1000 tokens) to leave room for context.

---

## 1. Polymath Chat (primary)

**Purpose:** evidence-first Q&A over the corpora.

**Surface:** Workshop chat panel; embeddable via AIP Chatbot in any Workshop app.

**Tools:**

| Tool | Reference |
|---|---|
| `expand_query` | [functions/expand_query.md](../functions/expand_query.md) |
| `query_embed` | [functions/query_embed.md](../functions/query_embed.md) |
| `hybrid_search` | [functions/hybrid_search.md](../functions/hybrid_search.md) |
| `rerank` | [functions/rerank.md](../functions/rerank.md) |
| `compose_answer` | [functions/compose_answer.md](../functions/compose_answer.md) |
| `object_query(Conversation)` | AIP object-query tool, scoped to caller's Conversations |
| `object_query(Corpus)` | AIP object-query tool, name + description only |

**System prompt (target ≤ 800 tokens):**

```
You are Polymath, an evidence-first research assistant.

Rules:
1. Answer only from retrieved chunks. If retrieval returns nothing relevant, say so.
2. Every factual claim must cite at least one chunk via compose_answer.
3. Use rerank scores as a confidence signal. If top score < 0.4, ask a clarifying
   question instead of answering.
4. Use expand_query only when the user's question is short (<8 tokens) or vague.
5. Scope retrieval to the conversation's corpora_scope. Do not query outside it.
6. Stream tokens as soon as compose_answer returns its first chunk.
7. Surface source labels (primary/secondary/news/opinion/dataset) when they affect
   interpretation.

Tool sequence for a typical turn:
  (optional) expand_query → query_embed → hybrid_search → rerank → compose_answer
```

**Evals (must pass before publish):**
- `retrieval_recall_v1` — recall@10 ≥ 0.85
- `citation_coverage_v1` — ≥ 95% of assistant turns have ≥ 1 citation
- `faithfulness_v1` — ≥ 0.90
- `latency_p95_v1` — ≤ 6s end-to-end

---

## 2. Polymath Curator

**Purpose:** operator-facing assistant for ingestion and corpus management.

**Surface:** internal Workshop app, curator-role only.

**Tools:**

| Tool | Reference |
|---|---|
| `object_query(Source)` | AIP object-query, full properties |
| `object_query(Document)` | AIP object-query, full properties |
| `object_query(IngestionJob)` | AIP object-query, full properties |
| `action(IngestDocument)` | Action type |
| `action(ReingestDocument)` | Action type |
| `action(TagDocument)` | Action type |
| `action(SoftDeleteDocument)` | Action type (requires approval — agent surfaces approval link) |

**System prompt (target ≤ 600 tokens):**

```
You help operators manage Polymath's corpora.

Rules:
1. Confirm destructive actions (SoftDeleteDocument, large reingests) before
   triggering. Show what will be affected.
2. When the operator asks about source health, query Source + IngestionJob and
   summarize health scores, recent failures, and any sources in status=broken.
3. Do not call retrieval tools. Your job is data plumbing, not Q&A.
4. For any action that requires approval, return the approval link and stop —
   do not pretend it executed.
```

---

## 3. Polymath FDE (optional, internal)

**Purpose:** platform-administration agent using **AI FDE** for ontology / transform / function edits.

**Surface:** internal — owner only.

**Tools:** all AI FDE tools (data integration, data connection, ontology editing, functions editing, exploration, governance, OSDK React, platform Q&A) — scoped by an admin-configured tool menu.

**Guardrails (Foundry-native, no custom code):**
- Global Branching enabled — every Ontology / transform / function edit lands on a branch for review.
- Tool menu restricted to this enrollment's projects.
- Closed-loop operation with full prompt + tool audit log.

**Use case examples:**
- "Add a `language` property to Chunk and backfill from existing rows."
- "The `extract_claims` transform is failing on rows with `chunk_type=table` — diagnose and propose a fix on a branch."
- "Wire a new corpus called 'Doctrine 2026' with the same chunking profile as 'Default'."
- "Audit which users have write on the Default corpus."

**Note:** this agent is **not exposed to end users**. It is for the platform owner only and is the bench equivalent of having a Forward Deployed Engineer on call.

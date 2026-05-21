# PRD — Polymath on Palantir Foundry

| | |
|---|---|
| **Version** | 0.2 — web freshness removed from v1 scope |
| **Date** | 2026-05-21 |
| **Author** | Kingsley |
| **Status** | Draft for Foundry build |
| **Source app** | Polymath RAG v3.3 (FastAPI + Qdrant + Neo4j + Pulsar) |

---

## 1. Executive summary

Polymath today is a research / knowledge-intelligence assistant: ingest a heterogeneous corpus (PDFs, HTML, web), retrieve with hybrid vector + lexical funnels, rerank with a cross-encoder, and answer with cited evidence. It runs as a self-hosted FastAPI stack on a local rig (RTX Pro 6000 Blackwell, 96 GB) behind a Cloudflare Tunnel.

This PRD specifies the rebuild on **Palantir Foundry + AIP**. The rebuild trades self-hosted server code for Foundry's governed data + AI platform:

- The **Ontology** becomes the single semantic surface for documents, chunks, entities, claims, and conversations.
- **AIP** hosts the LLM and provides the agent runtime.
- **Foundry transforms** replace the Python ingestion worker.
- **Foundry Functions** replace the orchestrator, funnels, and reranker.
- **AIP Chatbot Studio** replaces the chat backend + WebSocket relay.
- **Workshop** replaces the React operator UI.

Net effect: **no FastAPI server, no Qdrant cluster to operate, no Neo4j to maintain, no Docker compose, no auth middleware.** Code reduces to data pipelines (transforms) and pure logic units (Functions), all delivered in this folder as markdown.

---

## 2. Background

Polymath v3.3 ships with:

- 3 Qdrant collections (Naive / HRAG / Graph), all at 1024-dim, Qwen3-Embedding-0.6B for both ingest and query.
- Neo4j graph store backed by Apache Pulsar for async writes.
- Hybrid retrieval (funnel A broad + funnel B precision) with cross-encoder rerank (`ms-marco-MiniLM-L6-v2`).
- FastAPI orchestrator that coordinates retrieval, model calls (via OpenRouter), and citation assembly.
- React frontend with toggle bar (web on/off, HyDE on/off, etc.).
- A growing operational burden: 6+ services in compose, custom auth, source-label logic, and increasingly complex ingestion failure modes.

The system works, but the **operational surface area is large for one operator**. Foundry collapses that surface to managed services + governed code.

---

## 3. Goals

| ID | Goal |
|---|---|
| G1 | Move Polymath's corpus → retrieval → chat onto a single governed platform |
| G2 | Eliminate self-hosted server ops (FastAPI, Qdrant, Neo4j, Pulsar, compose) |
| G3 | Gain Foundry-native auth, lineage, audit, branching |
| G4 | Express retrieval and answer composition as AIP Agent tools over the Ontology |
| G5 | Preserve evidence-first answering with verifiable, queryable citations |
| G6 | Make every write a typed Action (no raw mutations) |
| G7 | Run an evals harness (AIP Evals) on schedule against an internal regression set |

## 4. Non-goals

- Multi-tenant SaaS — single org / enrollment.
- Fine-tuning LLMs — AIP hosts them.
- Mobile app — out of scope (The Council is separate).
- Web freshness / scheduled re-crawl — removed from v1 scope; can be added later.
- Reimplementing every v3.3 feature 1:1 — features that did not earn their keep stay behind.
- Backward compat with v3.3 APIs — we are cutting over, not bridging.

## 5. Personas

- **Operator / Researcher** (primary user — Kingsley). Asks questions, reviews answers + evidence, flags bad sources / claims.
- **Curator** (occasional). Ingests new corpora, tunes chunking, manages corpus lifecycle.
- **Platform owner** (you). Owns Ontology shape, Function code, agent prompts, eval suite.

## 6. Success metrics

| Metric | Target | Source |
|---|---|---|
| Retrieval recall@10 | ≥ 0.85 on internal eval set (parity with v3.3 funnel B) | AIP Evals |
| Answer faithfulness | ≥ 0.90 | AIP Evals |
| Citation coverage | ≥ 95% of assistant Messages have ≥ 1 Citation | Ontology query |
| End-to-end chat p95 latency | ≤ 6s for inputs ≤ 2k tokens | AIP Chatbot telemetry |
| Self-hosted services to operate | 0 | n/a |

## 7. Architecture overview

```
                ┌────────────────────────┐
   User ──────► │  AIP Chatbot Studio    │  (Polymath Chat agent)
                │  (system prompt + RAG) │
                └───────────┬────────────┘
                            │  tools
        ┌───────────────────┼───────────────────────┐
        ▼                   ▼                       ▼
   query_embed       hybrid_search             rerank
   (Function)        (Function:                (Function:
                      Vector Search +           cross-encoder)
                      lexical fusion, RRF)
                            │
                            ▼
                     compose_answer
                     (Function — writes Message
                      + Citation via Action)
                            │
                            ▼
                     ┌──────────────┐
                     │   Ontology   │  Conversation, Message, Citation,
                     │              │  Chunk, Document, Entity, Claim,
                     │              │  Source, Corpus, IngestionJob
                     └──────┬───────┘
                            ▲
        ┌───────────────────┴────────────────────────┐
        │     Foundry Python transforms (batch)      │
        │  ingest → chunk → embed → extract entities │
        │  → extract claims → source health          │
        └────────────────────────────────────────────┘
```

LLM calls happen **only** inside AIP (Chatbot system prompt + tool steering). No code in this repo calls an LLM directly.

## 8. Ontology summary

Full spec in [SCHEMA.md](SCHEMA.md). Headline:

- **11 object types**: Corpus, Source, Document, Chunk, Entity, Claim, Conversation, Message, Citation, IngestionJob, EvalRun
- **13 link types** wiring those together
- **2 interfaces**: `Evidence` (Chunk, Claim), `Indexable` (Document, Chunk)
- **7 action types** for governed mutations (see [actions/ACTION_TYPES.md](actions/ACTION_TYPES.md))

## 9. Functional requirements

### 9.1 Ingestion

- **FR-I1** — Operator can register a Source (URL or file).
- **FR-I2** — Triggering `IngestDocument` creates an IngestionJob → Document → Chunks via the transform chain.
- **FR-I3** — Chunks have 1024-dim embeddings indexed by Foundry Vector Search.
- **FR-I4** — Failures produce a tracked `IngestionJob` row with `status=failed` and an error string. No silent drops.
- **FR-I5** — Reingestion preserves `document_id`, bumps `version`, and replaces Chunks atomically.
- **FR-I6** — All file types in [INGESTION.md §1](INGESTION.md) are supported with type-specific parsers and structure-aware chunking.

### 9.2 Retrieval

- **FR-R1** — Hybrid retrieval: ANN over `Chunk.embedding` plus a lexical pass, fused by reciprocal-rank-fusion.
- **FR-R2** — Cross-encoder rerank of top-K candidates.
- **FR-R3** — Funnel A (broad recall) and Funnel B (precision-tuned) selectable per turn.
- **FR-R4** — HyDE query expansion toggleable (default off; on for short / vague queries).
- **FR-R5** — Multi-corpus scoping enforced at retrieval call time (`corpus_ids` parameter on `hybrid_search`).

### 9.3 Chat

- **FR-C1** — A single AIP Chatbot ("Polymath Chat") backed by retrieval + answer-composition tools.
- **FR-C2** — Conversations and Messages are first-class Ontology objects.
- **FR-C3** — Every assistant Message links to ≥ 1 Citation (no citation → flagged in evals).
- **FR-C4** — Streaming responses (AIP Chatbot native).
- **FR-C5** — Source labels (primary / secondary / news / opinion / dataset) are visible in citation UI.

### 9.4 Evidence & citations

- **FR-E1** — Citations show inline numbered references in the assistant Message.
- **FR-E2** — Clicking a citation expands the source Chunk + parent Document title + Source URL.
- **FR-E3** — Operator can flag a Claim (FlagClaim action). Flag propagates a badge to every Message that cites a Chunk supporting that Claim.

### 9.5 Multi-corpus

- **FR-M1** — Every Document belongs to exactly one Corpus.
- **FR-M2** — A Conversation can be scoped to one or many Corpora.
- **FR-M3** — `hybrid_search` enforces the scope by filtering Vector Search results on `corpus_id`.

### 9.6 Evals & observability

- **FR-V1** — AIP Evals harness runs the suite on demand and on schedule (nightly).
- **FR-V2** — EvalRun objects record per-suite metrics with lineage to the prompts and tools used.
- **FR-V3** — Foundry lineage tab shows transform → dataset → ontology → agent flow.

### 9.7 Governance

- **FR-G1** — Object-level and property-level ACLs on the Ontology, rooted at Corpus.
- **FR-G2** — All mutating Actions are audited; raw dataset writes never touch live Ontology object instances.
- **FR-G3** — Risky Actions (`PromoteToCanon`, `MergeEntities`, `SoftDeleteDocument`) require approval.
- **FR-G4** — Ontology edits via AI FDE land on a branch (Global Branching).

## 10. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-1 | All LLM calls go through AIP. No external API keys in code. |
| NFR-2 | All Ontology mutations are Action-typed; transforms write to staging datasets only. |
| NFR-3 | Embedding model is single + deterministic across ingest and query. |
| NFR-4 | Every Function added as an agent tool has AIP Evals coverage before going live. |
| NFR-5 | No PII in eval datasets; if synthetic data is needed, generate it. |
| NFR-6 | All transforms are idempotent on `(source_id, content_sha256)`. |

## 11. AIP agent design

See [agents/AIP_AGENTS.md](agents/AIP_AGENTS.md) for system prompts and tool lists. Summary:

- **Polymath Chat** — primary user-facing agent. Tools: `expand_query`, `hybrid_search`, `rerank`, `compose_answer`, object-query over `Conversation` / `Message` / `Corpus`.
- **Polymath Curator** — operator-facing agent for ingestion ops. Tools: object-query + the ingestion-related Actions.
- **Polymath FDE** (optional, internal) — AI FDE configured with platform tools, scoped to this enrollment's data integration + ontology editing + functions editing.

## 12. Migration plan

| Phase | Scope | Exit criteria |
|---|---|---|
| 0 — Tenant prep | Enable AIP, Vector Search, Global Branching on the Foundry tenant | Services healthy |
| 1 — Ontology stand-up | Create all object types, link types, interfaces on a branch | Schema review approved |
| 2 — Ingestion MVP | Land one small corpus end-to-end via transforms | 100% of corpus visible as Document + Chunk objects |
| 3 — Retrieval MVP | `query_embed` + `hybrid_search` + `rerank` Functions, smoke test | Recall@10 ≥ 0.7 on eval set |
| 4 — Chat MVP | AIP Chatbot wired to retrieval tools | First evidence-backed chat reply |
| 5 — Evidence + citations | Citation objects + inline citation UI in Workshop | Every answer has clickable citations |
| 6 — Multi-corpus | Per-corpus scoping enforced in retrieval and ingestion | Conversation scoping works end-to-end |
| 7 — Evals + governance | AIP Evals + approval gates on risky Actions | Eval CI runs nightly |
| 8 — Cutover | Stop v3.3 docker compose | Old stack decommissioned |

## 13. Open questions

- **OQ-1** — Which AIP-exposed embedding model do we pin? If not Qwen3-Embedding-0.6B-equivalent, accept a different one and re-eval recall.
- **OQ-2** — Foundry Vector Search vs storing vectors as object properties + a custom Function: which is cheaper at our scale (single-operator, ~500k chunks expected)?
- **OQ-3** — Keep HyDE? It helped on long-tail queries in v3.3.
- **OQ-4** — Source labels: typed enum property on Source, or separate `SourceLabel` object with curator-editable history? (Default: enum + audit log via Action.)
- **OQ-5** — Do we need a `Draft` vs `Canon` distinction on Document, or is "indexed" sufficient? (Default: keep both; canon needs approval to promote.)
- **OQ-6** — Where does graph retrieval (current v3.3 Funnel) fit? Likely as a Function over Entity link traversal, exposed as a separate agent tool.

## 14. Risks

| ID | Risk | Mitigation |
|---|---|---|
| R-1 | AIP-hosted LLM behavior differs from OpenRouter pipelines used in v3.3 | Carry the eval set across migration; re-tune prompts; pin a model |
| R-2 | Foundry Vector Search recall may differ from Qdrant tuning | Same — eval set is portable |
| R-3 | Branching adds latency to schema changes | Accepted — this is the price of governance |
| R-4 | Function quotas on AIP may cap parallelism | Profile early in Phase 3 |
| R-5 | Ingestion of large PDFs (Docling) may need external compute | Use Foundry's compute profiles; fall back to a sidecar if needed |

## 15. Appendix — Foundry primitives reused

- **Foundry Pipeline Builder / Python transforms** — ingest pipelines
- **Foundry Vector Search Service** — chunk vector storage + ANN
- **Object Storage V2** — Ontology backing store
- **AIP Logic** — deterministic agent functions
- **AIP Chatbot Studio** — conversational agent runtime
- **AIP Evals** — eval harness
- **Workshop** — operator UI (ingestion, citation viewer, flagging)
- **Global Branching** — Ontology and transform branching for safe edits
- **AI FDE** — optional platform-administration agent
- **OSDK** — only if an external app is ever needed (out of scope for v1)

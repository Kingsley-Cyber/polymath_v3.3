# Polymath on Palantir Foundry

This folder is the design + reference scaffolding for porting **Polymath RAG v3.3** (FastAPI + Qdrant + Neo4j + Docker compose) onto **Palantir Foundry + AIP**.

> **DoD-laptop note:** every code artifact in this folder is delivered as a `.md` file with the code inside a fenced ```python``` block — not a `.py`. Copy the code block into a real `.py` on the Foundry side.

## Read in this order

1. [PRD.md](PRD.md) — what we're building and why
2. [RATIONALE.md](RATIONALE.md) — top-level design intent
3. [SCHEMA.md](SCHEMA.md) — the Ontology (object types, link types, interfaces, action types)
4. [GRAPH_SCHEMA.md](GRAPH_SCHEMA.md) — Neo4j-idiom graph schema + Foundry Ontology mapping
5. [INGESTION.md](INGESTION.md) — semantic ingestion design for every file type
6. [QUERY.md](QUERY.md) — retrieval + answer-composition pipeline
7. [STORAGE.md](STORAGE.md) — where every piece of data lives
8. [METADATA.md](METADATA.md) — full metadata catalog
9. [COMPONENTS.md](COMPONENTS.md) — index of every logical component
10. [actions/ACTION_TYPES.md](actions/ACTION_TYPES.md) — governed mutations
11. [agents/AIP_AGENTS.md](agents/AIP_AGENTS.md) — AIP Chatbot / Agent specs
12. `transforms/*.md` and `functions/*.md` — code-as-markdown for each transform / function

## What changes vs Polymath v3.3

| v3.3 today | Foundry equivalent |
|---|---|
| FastAPI server (`backend/`) | AIP Chatbot Studio + Workshop apps (no server code) |
| Qdrant vector store (3 collections) | Foundry Vector Search Service indexing `Chunk.embedding` |
| Neo4j graph cluster | Ontology object types + link types (graph IS the ontology) |
| MongoDB ingestion state | Object Storage V2 (Ontology backing) |
| Ingestion worker (`backend/services/ingestion/worker.py`) | Foundry Python transforms (scheduled / action-triggered) |
| Chat orchestrator (`backend/services/chat_orchestrator.py`) | AIP Logic + AIP Chatbot Studio |
| Funnels A/B + reranker | Foundry Functions called as agent tools |
| Auth (bearer token, `~/.polymath-dev-token`) | Foundry SSO + RBAC (platform-handled) |
| WebSocket streaming | AIP Chatbot streams natively |
| `docker-compose.yml` (Qdrant, Neo4j, Pulsar, SearXNG, Docling) | Foundry-managed services + Pipeline Builder |
| OpenRouter / model clients | AIP-hosted LLMs (tool-callable) |
| Source label logic | Property + computed `health_score` on `Source` |

## What stays as code (delivered as `.md`)

- `transforms/` — batch pipelines: ingest, chunk, embed, extract entities, extract claims, source health
- `functions/` — synchronous logic units: query embedding, query expansion, hybrid search, rerank, answer composition, eval harness

**No FastAPI. No uvicorn. No model client. No Qdrant / Neo4j drivers. No WebSocket relay.** The platform owns all of that.

## Folder map

```
polymath-foundry/
├── README.md                 # this file
├── PRD.md                    # product requirements
├── RATIONALE.md              # design intent ("why")
├── SCHEMA.md                 # Ontology object/link/action spec
├── GRAPH_SCHEMA.md           # Neo4j-idiom view + Foundry mapping
├── INGESTION.md              # per-file-type ingestion + semantic chunking
├── QUERY.md                  # retrieval + answer pipeline
├── STORAGE.md                # data lifecycle, branching, ACLs
├── METADATA.md               # full metadata catalog
├── COMPONENTS.md             # logical-component index
├── actions/
│   └── ACTION_TYPES.md       # action types in detail
├── agents/
│   └── AIP_AGENTS.md         # agent specs (chat, curator, FDE)
├── transforms/               # code-as-markdown for each transform
│   ├── ingest_documents.md
│   ├── chunk_documents.md
│   ├── embed_chunks.md
│   ├── extract_entities.md
│   ├── extract_claims.md
│   └── compute_source_health.md
└── functions/                # code-as-markdown for each Foundry Function
    ├── query_embed.md
    ├── expand_query.md
    ├── hybrid_search.md
    ├── rerank.md
    ├── compose_answer.md
    └── run_eval.md
```

## Drop-in conventions

- Foundry dataset paths use `/Polymath/...` (rename to match your tenant's project root).
- All transforms use the `@transform` decorator from `transforms.api`.
- All Functions use the `@function` decorator from `functions.api`.
- Ontology object imports come from the generated `ontology.objects` module Foundry produces for your tenant.
- Vector dimension is **1024** to preserve parity with v3.3 (Qwen3-Embedding-0.6B). If AIP exposes a different default, re-eval recall before swapping.

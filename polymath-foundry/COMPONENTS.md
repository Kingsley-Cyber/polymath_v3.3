# Components — Polymath on Foundry

The index of every logical component. Two categories:

| Category | Folder | Runs when | Triggered by |
|---|---|---|---|
| **Transforms** | `transforms/` | Batch | Schedule or Action |
| **Functions** | `functions/` | Synchronous, on demand | AIP Agent tool call, Action, Workshop |

Each component below has a code-as-markdown reference file. Nothing in this folder is a server — AIP hosts the LLM and Foundry runs the rest.

---

## Transforms

| Reference | Purpose | Input dataset | Output dataset | Triggered by |
|---|---|---|---|---|
| [transforms/ingest_documents.md](transforms/ingest_documents.md) | Convert raw artifacts into Document rows. Computes `content_sha256`. Generates per-document summary. | `/Polymath/raw/sources` | `/Polymath/clean/documents` | Action `IngestDocument` |
| [transforms/chunk_documents.md](transforms/chunk_documents.md) | Semantic chunking (structure-aware: heading, paragraph, table, list, code). | `/Polymath/clean/documents` | `/Polymath/clean/chunks` | Auto, after ingest |
| [transforms/embed_chunks.md](transforms/embed_chunks.md) | 1024-dim embeddings via AIP. Registers vectors with Vector Search. | `/Polymath/clean/chunks` | `/Polymath/clean/chunks_embedded` (+ Vector Search index) | Auto, after chunk |
| [transforms/extract_entities.md](transforms/extract_entities.md) | NER + alias resolution. Produces Entity rows + Chunk→Entity links. | `/Polymath/clean/chunks_embedded` | `/Polymath/clean/entities`, `/Polymath/links/chunk_mentions_entity` | Auto |
| [transforms/extract_claims.md](transforms/extract_claims.md) | Subject-predicate-object claim extraction with confidence. | `/Polymath/clean/chunks_embedded` | `/Polymath/clean/claims`, `/Polymath/links/chunk_supports_claim` | Auto |
| [transforms/compute_source_health.md](transforms/compute_source_health.md) | Per-source health (success rate + latency) from IngestionJob outcomes. | `/Polymath/ops/ingestion_jobs` | `/Polymath/clean/sources` (health column) | Schedule, daily |

---

## Functions

| Reference | Purpose | Inputs | Output | Called by |
|---|---|---|---|---|
| [functions/query_embed.md](functions/query_embed.md) | Embed a user query into the same 1024-dim space as Chunks. | `query: str` | `vector: list[float]` | `hybrid_search` |
| [functions/expand_query.md](functions/expand_query.md) | HyDE-style query expansion. | `query: str` | `expanded: list[str]` | Optional pre-step |
| [functions/hybrid_search.md](functions/hybrid_search.md) | Vector ANN + lexical fusion (RRF) over Chunks. | `query: str, corpus_ids: list[str], k: int` | `chunks: list[Chunk]` | AIP Chatbot tool |
| [functions/rerank.md](functions/rerank.md) | Cross-encoder rerank of candidate chunks. | `query: str, chunks: list[Chunk]` | reordered `chunks` | After `hybrid_search` |
| [functions/compose_answer.md](functions/compose_answer.md) | Compose answer text + write Message + Citations via Action. | `query, chunks, conversation_id` | `message_id` | AIP Chatbot final step |
| [functions/run_eval.md](functions/run_eval.md) | Execute eval suite; write EvalRun object. | `suite_name: str` | `eval_run_id: str` | AIP Evals harness; schedule |

---

## Calling pattern — chat turn

```
User turn ──► AIP Chatbot (Polymath Chat)
                  │
                  ├── (optional) tool: expand_query    ──► functions/expand_query
                  ├── tool: query_embed                ──► functions/query_embed
                  ├── tool: hybrid_search              ──► functions/hybrid_search
                  ├── tool: rerank                     ──► functions/rerank
                  └── tool: compose_answer             ──► functions/compose_answer
                                                            │
                                                            ▼
                                                   Action: CreateMessageWithCitations
                                                            │
                                                            ▼
                                                   Ontology: Message + Citation objects
```

## Calling pattern — ingestion

```
Operator triggers Action: IngestDocument
        │
        ▼
Row appended to /Polymath/raw/sources
        │
        ▼
transforms/ingest_documents     (creates Document rows + summaries)
        │
        ▼
transforms/chunk_documents      (semantic chunking → Chunks)
        │
        ▼
transforms/embed_chunks         (writes embeddings; indexes in Vector Search)
        │
        ├──► transforms/extract_entities
        └──► transforms/extract_claims
```

---

## What's deliberately NOT here

| Removed file class | Why |
|---|---|
| `main.py` / `app.py` / `server.py` | No process to run — Foundry/AIP hosts the runtime |
| `auth.py` / middleware | Foundry SSO + RBAC |
| `openrouter_client.py` / model clients | AIP exposes models; tools call AIP |
| `qdrant_writer.py` / `qdrant_reader.py` | Foundry Vector Search replaces Qdrant |
| `neo4j_client.py` / Cypher files | Ontology link types replace Neo4j |
| `pulsar_producer.py` / `pulsar_consumer.py` | Foundry's own scheduling / triggers |
| `websocket_relay.py` | AIP Chatbot streams natively |
| `searxng_client.py` | If web search is needed, AIP web tool + a Function wrapper replaces it |
| `web_freshness_crawl.py` | Removed from v1 scope |
| Frontend (`frontend/`) | Workshop or an OSDK React app — out of this folder's scope |

---

## Note on file format

Every code component above is delivered as a `.md` file with the Python in a fenced ```python``` block, plus a docstring / metadata section above. This format passes DoD laptop file-type filters that may block `.py`. To use, paste the code block into the equivalent `.py` on the Foundry side.

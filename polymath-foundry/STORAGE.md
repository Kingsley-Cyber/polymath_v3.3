# Storage — Polymath on Foundry

Where every piece of data lives, how it moves, who can see it. Goal: one governed surface, zero self-hosted state.

---

## 1. Data layers

| Layer | What lives there | Backing |
|---|---|---|
| **Raw** | Source artifacts (PDFs, HTML dumps, blobs uploaded by operators) | Foundry datasets under `/Polymath/raw/...` |
| **Clean** | Normalized Document + Chunk rows (post-parse, pre-Ontology) | Foundry datasets under `/Polymath/clean/...` |
| **Links** | Edge rows (Chunk→Entity, Chunk→Claim, etc.) used to materialize Ontology link types | Foundry datasets under `/Polymath/links/...` |
| **Ontology** | Live, governed object instances | Object Storage V2 |
| **Vector index** | 1024-dim Chunk embeddings | Foundry Vector Search Service, index `polymath_chunks` |
| **Ops** | IngestionJob rows, EvalRun rows, audit trails | Foundry datasets + native audit log |
| **Conversations** | Conversation, Message, Citation objects | Object Storage V2 |

Raw → Clean → Links transformations live entirely in transforms. The Clean / Links datasets back the Ontology object types — the Ontology Manager binds dataset columns to object properties.

---

## 2. Dataset paths

```
/Polymath/
├── raw/
│   ├── sources              # rows for each Action: IngestDocument
│   └── uploads/             # blob storage for uploaded files
├── clean/
│   ├── corpora              # Corpus objects (small dataset)
│   ├── sources              # Source objects
│   ├── documents            # Document objects
│   ├── chunks               # Chunk rows (text + metadata, no embeddings yet)
│   ├── chunks_embedded      # Chunk rows + embeddings (backs the Chunk object)
│   ├── entities             # Entity objects
│   └── claims               # Claim objects
├── links/
│   ├── chunk_mentions_entity
│   ├── chunk_supports_claim
│   └── entity_related_to_entity
├── ops/
│   ├── ingestion_jobs       # IngestionJob objects
│   └── eval_runs            # EvalRun objects
└── conversations/
    ├── conversations
    ├── messages
    └── citations
```

Rename `/Polymath/` to match your tenant's project root.

---

## 3. Lifecycle of a Document

```
Action: IngestDocument
        │
        ▼
raw/sources row appended           (1)
        │
        ▼
clean/documents row created        (2)  status=draft
        │
        ▼
clean/chunks rows created          (3)
        │
        ▼
chunks_embedded + Vector Search    (4)
        │
        ▼
entities + claims + links          (5)
        │
        ▼
Document.status = "indexed"        (6)  visible to retrieval
        │
        ▼  (curator action)
PromoteToCanon                     (7)  status=canon  (requires approval)
        │
        ▼  (curator action)
SoftDeleteDocument                 (8)  status=archived (requires approval)
```

`archived` Documents remain queryable via lineage but are excluded from retrieval (Vector Search gates on `status != "archived"`).

---

## 4. Lifecycle of a Conversation

```
First user turn
        │
        ▼
Conversation object created  (owner = current user, corpora_scope set)
        │
        ▼
For each turn:
    Message (user) appended  ──► has_messages link
    Functions run
    Action: CreateMessageWithCitations
        Message (assistant) + Citations created atomically
        │
        ▼
Conversation.message_count and last_message_at updated by derived transform
```

Conversations are owned. Sharing is explicit (Workshop share dialog → Foundry ACL grant).

---

## 5. ACLs

| Object | Default scope | Who can read | Who can write |
|---|---|---|---|
| Corpus | Org-default → restricted by Corpus.owner | Members granted on Corpus | Owner + curator role |
| Document | Inherits from Corpus | Inherits | Curator role |
| Chunk | Inherits from Document | Inherits | Transforms only (via dataset write); no direct writes |
| Entity | Org-wide | All members | Curator (via MergeEntities action) |
| Claim | Org-wide | All members | Curator (FlagClaim action only — extractor writes are dataset-level) |
| Conversation | Owner only | Owner + explicitly shared users | Owner |
| Message | Inherits from Conversation | Inherits | Functions only (via Action) |
| Citation | Inherits from Message | Inherits | Functions only |
| IngestionJob | Curator + operator | Both | Transforms only |
| EvalRun | Platform admin | Admin | Eval harness only |

Property-level ACL exceptions:
- `Claim.flag_reason` — admin-only (avoids leaking moderation notes).
- `Source.notes` — curator-only (operator notes about source quality).

---

## 6. Branching

| Change | Where the branch lives | Reviewer | Merge path |
|---|---|---|---|
| Ontology schema (new object type / property) | Ontology branch | Schema reviewer | Promote branch to main after review |
| Action type definition | Ontology branch | Reviewer + AIP Evals must pass | Same |
| Transform code | Standard Foundry CI branch | Reviewer | Standard CI |
| Function code | Standard Foundry CI branch | Reviewer for tool-exposed Functions | Standard CI |
| Agent system prompt | Foundry branch | Reviewer + AIP Evals pass on the suite | Standard CI |
| AI FDE-proposed change | Always on a branch | Reviewer | Owner merges |

**Why Global Branching matters here:** the AI FDE agent operates the platform. Without branching, a bad LLM-proposed Ontology edit could land in production. With branching, every proposal is reviewable and revertable.

---

## 7. Retention

| Object | Retention default | Purge policy |
|---|---|---|
| Source artifact (raw blob) | 1 year | Configurable per Corpus |
| Document (`status=archived`) | Indefinite (cheap; small) | Manual purge only |
| Chunk (orphaned by Document delete) | Removed in next cleanup transform | Auto |
| Entity / Claim | Indefinite | Manual via `MergeEntities` |
| Conversation / Message / Citation | 2 years (owner-configurable) | Auto after window |
| IngestionJob | 90 days | Auto |
| EvalRun | Indefinite (small, valuable) | Manual |
| Audit log | Per Foundry policy | Platform-managed |

---

## 8. Backup

Foundry handles backup at the platform layer. No additional logic in this design.

What *is* in scope here:

- All transforms are idempotent and re-runnable from raw, so a re-derivation from Raw datasets is the recovery path for anything in Clean / Links / Ontology.
- Vector Search index is rebuildable from `chunks_embedded` at any time.
- Conversations have no upstream and require Foundry's own backup.

---

## 9. Intent + Reasoning

- **Why Raw / Clean / Links / Ontology layering.** The same separation as a data warehouse: Raw is the only authoritative source; Clean and Links are derivable; Ontology is the read surface. Re-derivation is cheap (Foundry pipelines) and the source of truth is small and easy to back up.
- **Why vector index is a service, not a property.** Storing 1024-dim floats inside an object backing dataset works for small N. At ~500k chunks, that's 500k × 1024 × 4 bytes = 2 GB of embeddings; ANN over that without an index would be 100s of ms per query. Foundry Vector Search is the right tool.
- **Why ACLs root at Corpus.** Corpus is the unit a real human reasons about ("this project's data"). Rolling ACLs down through Document / Chunk follows the operator's mental model and avoids per-Chunk ACL grants.
- **Why branching is mandatory for schema, not just for code.** Schema changes are the highest-blast-radius changes in the system — they affect every downstream Function and Workshop view. Even good schema changes need to be staged.
- **Why ingestion is idempotent.** Re-running a transform should not produce duplicate Chunks. `(source_id, content_sha256)` is the natural key, and Foundry's `upsert_by_primary_key` write mode enforces it.
- **Why we keep `archived` Documents queryable by lineage.** Audit and legal. A citation to a now-archived Document still resolves to a viewable record — the chunk is hidden from retrieval but the trace exists.
- **Why Conversations live in the same store as Documents.** Same ACL engine, same audit log, same query surface. Splitting them across stores (e.g. Conversation in a different DB) would replicate the v3.3 mistake of three sources of truth.

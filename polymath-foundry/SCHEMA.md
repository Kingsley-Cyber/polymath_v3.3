# Polymath Foundry Ontology — Schema

The source of truth for the Polymath Ontology. All object types, link types, interfaces, and action types live here as a spec. Implementation lands in Foundry itself via the Ontology Manager (or AI FDE, on a branch).

For the same shape expressed in **Neo4j / Cypher idiom** plus traversal examples, see [GRAPH_SCHEMA.md](GRAPH_SCHEMA.md).

---

## 1. Design principles

1. **Evidence is first-class.** Citation, Chunk, and Claim are queryable objects with lineage — not strings in a JSON blob.
2. **No raw writes.** Anything that mutates the Ontology goes through an Action type with validation.
3. **Embeddings live next to chunks.** Each Chunk has a `vector(1024)` property; Vector Search indexes it. No external Qdrant.
4. **Conversations are auditable.** Every Message → Citation → Chunk → Document chain is traversable in either direction.
5. **Multi-corpus from day one.** Every Document belongs to exactly one Corpus. Corpus is the security boundary.
6. **Idempotent ingestion.** `(source_id, content_sha256)` is the natural key for change detection.

---

## 2. Object types

### 2.1 `Corpus`
The top-level grouping. The security boundary for all downstream objects.

| Property | Type | Notes |
|---|---|---|
| `corpus_id` (PK) | string | uuid |
| `name` | string | display name, unique |
| `description` | text | |
| `owner` | user reference | RBAC root |
| `created_at` | timestamp | |
| `document_count` | integer | derived; refreshed by transform |
| `default_chunking_profile` | enum {fine, balanced, coarse} | applied at ingest unless overridden |

### 2.2 `Source`
Where a Document came from. One Source can produce many Documents over time (versions).

| Property | Type | Notes |
|---|---|---|
| `source_id` (PK) | string | uuid |
| `kind` | enum {web, upload, api, drive} | |
| `uri` | string | URL or path |
| `label` | enum {primary, secondary, news, opinion, dataset} | trust label |
| `last_fetched_at` | timestamp | |
| `health_score` | float | 0–1, computed (success rate + latency) |
| `status` | enum {active, paused, broken} | |
| `notes` | text | curator-editable; admin-readable only |

### 2.3 `Document`
A single ingested artifact. Versioned.

| Property | Type | Notes |
|---|---|---|
| `document_id` (PK) | string | uuid; stable across versions |
| `title` | string | |
| `corpus_id` | string | FK |
| `source_id` | string | FK |
| `content_sha256` | string | natural key for change detection |
| `ingested_at` | timestamp | |
| `version` | integer | bumped on reingest |
| `status` | enum {draft, indexed, canon, archived} | |
| `token_count` | integer | |
| `tags` | array<string> | free-form, curator-managed |
| `summary` | text | ≤ 200-token executive summary, LLM-generated at ingest |

### 2.4 `Chunk`
A segment of a Document with an embedding.

| Property | Type | Notes |
|---|---|---|
| `chunk_id` (PK) | string | uuid |
| `document_id` | string | FK |
| `corpus_id` | string | denormalized for Vector Search facet |
| `ordinal` | integer | position within document |
| `text` | text | |
| `token_count` | integer | |
| `embedding` | vector(1024) | indexed by Vector Search Service |
| `chunk_type` | enum {paragraph, table, list, code, heading} | |
| `headings` | array<string> | heading path; e.g. ["Doctrine", "Phase II"] |
| `page` | integer | nullable; PDF only |
| `bbox` | json | nullable; PDF only |
| `start_ms` / `end_ms` | integer | nullable; audio only |

### 2.5 `Entity`
Named entity extracted from chunks.

| Property | Type | Notes |
|---|---|---|
| `entity_id` (PK) | string | uuid |
| `canonical_name` | string | |
| `aliases` | array<string> | merged in MergeEntities |
| `entity_type` | enum {person, org, place, system, concept, event, product, doctrine} | |
| `canonical_uri` | string | nullable; e.g., Wikidata QID |
| `description` | text | |
| `merged_into` | string | nullable; points to surviving entity when this one is archived |

### 2.6 `Claim`
A subject-predicate-object assertion with provenance.

| Property | Type | Notes |
|---|---|---|
| `claim_id` (PK) | string | uuid |
| `statement` | text | natural-language form |
| `subject_entity_id` | string | nullable FK |
| `object_entity_id` | string | nullable FK |
| `predicate` | string | natural-language predicate |
| `confidence` | float | extractor confidence 0–1 |
| `flagged` | boolean | set by FlagClaim |
| `flag_reason` | text | nullable; property-ACL: admin-only |
| `flagged_by` | user reference | nullable |

### 2.7 `Conversation`
A chat session.

| Property | Type | Notes |
|---|---|---|
| `conversation_id` (PK) | string | uuid |
| `owner` | user reference | |
| `title` | string | auto-generated from first turn |
| `corpora_scope` | array<string> | FKs to Corpus |
| `created_at` | timestamp | |
| `message_count` | integer | derived |
| `last_message_at` | timestamp | derived |

### 2.8 `Message`
A single turn.

| Property | Type | Notes |
|---|---|---|
| `message_id` (PK) | string | uuid |
| `conversation_id` | string | FK |
| `role` | enum {user, assistant, system} | |
| `content` | text | |
| `created_at` | timestamp | |
| `latency_ms` | integer | nullable; assistant turns only |
| `token_count` | integer | nullable |
| `tools_used` | array<string> | function names called this turn |

### 2.9 `Citation`
A pointer from an assistant Message to a Chunk that supports it.

| Property | Type | Notes |
|---|---|---|
| `citation_id` (PK) | string | uuid |
| `message_id` | string | FK |
| `chunk_id` | string | FK |
| `ordinal` | integer | inline number (1, 2, 3…) |
| `span_start` | integer | char offset within chunk text |
| `span_end` | integer | char offset within chunk text |
| `rerank_score` | float | score at the time of citation |

### 2.10 `IngestionJob`
Operational record of a transform run.

| Property | Type | Notes |
|---|---|---|
| `job_id` (PK) | string | uuid |
| `source_id` | string | FK |
| `status` | enum {queued, running, succeeded, failed} | |
| `started_at` | timestamp | |
| `finished_at` | timestamp | nullable |
| `documents_created` | integer | |
| `chunks_created` | integer | |
| `error` | text | nullable |
| `triggered_by_action` | string | nullable; action audit ID |

### 2.11 `EvalRun`
A run of the eval suite.

| Property | Type | Notes |
|---|---|---|
| `eval_run_id` (PK) | string | uuid |
| `suite_name` | string | e.g., `retrieval_recall_v1` |
| `started_at` | timestamp | |
| `finished_at` | timestamp | nullable |
| `metrics` | json | recall@k, faithfulness, etc. |
| `commit_ref` | string | code / prompt version |
| `passed` | boolean | suite-level pass/fail |

---

## 3. Link types

| Name | From | To | Cardinality | Notes |
|---|---|---|---|---|
| `has_chunks` | Document | Chunk | 1..n | implicit via Chunk.document_id |
| `from_source` | Document | Source | n..1 | implicit via Document.source_id |
| `belongs_to_corpus` | Document | Corpus | n..1 | enforces multi-corpus scoping |
| `mentions` | Chunk | Entity | n..m | stored; carries span + score |
| `supports` | Chunk | Claim | n..m | stored; carries score |
| `subject_of` | Claim | Entity | n..1 | implicit |
| `object_of` | Claim | Entity | n..1 | implicit |
| `related_to` | Entity | Entity | n..m | stored; carries predicate + confidence |
| `has_messages` | Conversation | Message | 1..n | implicit |
| `cites` | Message | Citation | 1..n | assistant Messages only |
| `evidence_chunk` | Citation | Chunk | n..1 | implicit |
| `produced_by` | Document | IngestionJob | n..1 | |
| `scoped_to` | Conversation | Corpus | n..m | from Conversation.corpora_scope |

---

## 4. Interfaces

### 4.1 `Evidence`
Implemented by: `Chunk`, `Claim`.

Common surface:
- `text: string`
- `confidence: float` (Chunk's confidence is 1.0; Claim's is the extractor confidence)
- `source_uri: string` (resolved by interface implementation)

Why: Functions like `cite()` and `compose_answer()` accept any `Evidence` without caring whether it's a Chunk or a Claim.

### 4.2 `Indexable`
Implemented by: `Document`, `Chunk`.

Common surface:
- `text: string`
- `token_count: integer`
- `embedding: vector(1024)` (Document's is the centroid of its chunks; nullable)

Why: The embedding transform processes either uniformly; downstream tools can query either.

---

## 5. Action types

Full specs in [actions/ACTION_TYPES.md](actions/ACTION_TYPES.md). Summary:

| Action | Inputs | Effect | Approval |
|---|---|---|---|
| `IngestDocument` | source_id, corpus_id | Creates IngestionJob; triggers transforms | No |
| `ReingestDocument` | document_id | Bumps version; re-runs transforms | No |
| `MergeEntities` | primary_id, duplicate_ids[] | Archives duplicates; rewires links | Yes |
| `FlagClaim` | claim_id, reason | Sets flagged=true; propagates UI badge | No |
| `PromoteToCanon` | document_id | Status: indexed → canon | Yes |
| `TagDocument` | document_id, tags[] | Updates tags property | No |
| `SoftDeleteDocument` | document_id, reason | Status: any → archived | Yes |

---

## 6. Security model

- **Corpus is the security boundary.** ACLs on Corpus cascade to its Documents, Chunks, Entities (via mentions), and Claims (via supports).
- **Citation** inherits security from its underlying Chunk.
- **Conversation** is private to its owner unless explicitly shared.
- **EvalRun** is platform-admin only.
- **Property-level ACLs**: `Claim.flag_reason` is admin-only; `Source.notes` is curator-only.

---

## 7. Vector indexing

- Foundry Vector Search Service indexes `Chunk.embedding` (1024-dim).
- Index key: `chunk_id`.
- Facets for filtering at query time: `corpus_id`, `document_id`, `chunk_type`.
- ANN is the first pass; rerank narrows. Final top-k cited is typically 3–8.

---

## 8. Branching strategy

| Change | Branch required | Approval |
|---|---|---|
| New object type or property | Yes | Reviewer |
| New action type | Yes | Reviewer |
| Function code change | Standard Foundry CI | Reviewer for tool-exposed functions |
| Transform code change | Standard Foundry CI | Reviewer |
| Agent system prompt change | Yes | Reviewer + AIP Evals must pass |
| Anything proposed by AI FDE | Yes (always lands on branch) | Reviewer |

---

## 9. Initial seed data

On first stand-up:

- One Corpus: `"Default"`
- Source labels enum seeded: `primary, secondary, news, opinion, dataset`
- Chunking profiles: `fine` (256 tok / 32 overlap), `balanced` (512 / 64), `coarse` (1024 / 128)
- No seed Documents — pipeline lands them.

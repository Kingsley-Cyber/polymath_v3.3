# Metadata — Polymath on Foundry

Every property on every object, every link, every derived field. Catalog form, so you can find any data point and know who sets it, when, and who reads it.

---

## 1. Conventions

| Column | Meaning |
|---|---|
| **Property** | Field name |
| **Type** | string / integer / float / boolean / timestamp / enum / vector / array / json / user / FK |
| **Set by** | Which transform or Action populates it |
| **Set when** | At what stage |
| **Read by** | Which retrieval / UI surface uses it |
| **Notes** | Defaults, derivations, ACLs |

---

## 2. `Corpus`

| Property | Type | Set by | Set when | Read by | Notes |
|---|---|---|---|---|---|
| `corpus_id` | string (uuid) | Curator (Workshop "New Corpus" form) | Creation | Everywhere | PK |
| `name` | string | Curator | Creation; editable | Workshop, agent prompts | Unique per org |
| `description` | text | Curator | Creation; editable | Workshop, agent corpus picker | |
| `owner` | user | Auto | Creation | ACL engine | Defaults to creating user |
| `created_at` | timestamp | Auto | Creation | Workshop sort | |
| `document_count` | integer | Derived transform | Daily | Workshop dashboard | `count(Document where corpus_id=this.id)` |
| `default_chunking_profile` | enum {fine, balanced, coarse} | Curator | Creation; editable | `chunk_documents` transform | Default `balanced` |

---

## 3. `Source`

| Property | Type | Set by | Set when | Read by | Notes |
|---|---|---|---|---|---|
| `source_id` | string (uuid) | Action `IngestDocument` (or curator on create) | Creation | Everywhere | PK |
| `kind` | enum {web, upload, api, drive} | Curator | Creation | `ingest_documents` parser dispatch | |
| `uri` | string | Curator | Creation; editable | Citation UI (display) | |
| `label` | enum {primary, secondary, news, opinion, dataset} | Curator | Creation; editable | Citation UI badge, retrieval agent system prompt | |
| `last_fetched_at` | timestamp | `ingest_documents` transform | On each ingest | Curator dashboard | |
| `health_score` | float | `compute_source_health` transform | Daily | Curator dashboard | `0.7 * success_rate_30d + 0.3 * latency_score` |
| `status` | enum {active, paused, broken} | Curator + auto | Creation; auto on repeated failure | Ingestion scheduler, curator UI | `broken` after 3 consecutive failures |
| `notes` | text | Curator | On demand | Curator UI only | Property-level ACL: curator-only |

---

## 4. `Document`

| Property | Type | Set by | Set when | Read by | Notes |
|---|---|---|---|---|---|
| `document_id` | string (uuid) | `ingest_documents` | First ingest | Everywhere | PK; stable across versions |
| `title` | string | `ingest_documents` | Each version | Citation UI, retrieval | Derived from parser; editable in curator UI |
| `corpus_id` | FK Corpus | Action `IngestDocument` | Creation | Retrieval scoping, ACLs | |
| `source_id` | FK Source | Action `IngestDocument` | Creation | Citation UI | |
| `content_sha256` | string | `ingest_documents` | Each ingest | Idempotency check | Natural key |
| `ingested_at` | timestamp | `ingest_documents` | Each version | UI sort | |
| `version` | integer | `ingest_documents` | Each ingest | Curator UI | Starts at 1 |
| `status` | enum {draft, indexed, canon, archived} | Transforms + Actions | Lifecycle | Retrieval gating | See [STORAGE.md §3](STORAGE.md) |
| `token_count` | integer | `ingest_documents` | Each version | Curator dashboard | |
| `tags` | array<string> | Action `TagDocument` | On demand | Curator UI, optional retrieval filter | Max 32 |
| `summary` | text | `ingest_documents` summary stage | Each version | Citation hover, document reranker | ≤ 200 tokens |

---

## 5. `Chunk`

| Property | Type | Set by | Set when | Read by | Notes |
|---|---|---|---|---|---|
| `chunk_id` | string (uuid) | `chunk_documents` | Each chunk | Everywhere | PK |
| `document_id` | FK Document | `chunk_documents` | Creation | Retrieval, citation | |
| `corpus_id` | FK Corpus (denormalized) | `chunk_documents` | Creation | Vector Search facet | Denormalized for query-time filter |
| `ordinal` | integer | `chunk_documents` | Creation | Citation UI order | Position in document |
| `text` | text | `chunk_documents` | Creation | Lexical search, rerank input | |
| `token_count` | integer | `chunk_documents` | Creation | Compose-time budget | |
| `embedding` | vector(1024) | `embed_chunks` | After chunk | Vector Search | Pinned model |
| `chunk_type` | enum {paragraph, table, list, code, heading} | `chunk_documents` | Creation | Vector Search facet, UI rendering | |
| `headings` | array<string> | `chunk_documents` | Creation | Citation UI context | Heading path; e.g. ["Doctrine", "Phase II"] |
| `page` | integer | `chunk_documents` | PDFs only | Citation UI | Nullable |
| `bbox` | json | `chunk_documents` | PDFs only | Citation UI (highlight box) | Nullable |
| `start_ms` / `end_ms` | integer | `chunk_documents` | Audio only | Citation UI (playback) | Nullable |

---

## 6. `Entity`

| Property | Type | Set by | Set when | Read by | Notes |
|---|---|---|---|---|---|
| `entity_id` | string (uuid) | `extract_entities` | First mention | Everywhere | PK |
| `canonical_name` | string | `extract_entities` + curator | Creation; editable | UI, graph traversal | |
| `aliases` | array<string> | `extract_entities` + `MergeEntities` | Creation; on merge | NER resolver | |
| `entity_type` | enum | `extract_entities` | Creation | UI, agent tools | |
| `canonical_uri` | string | Curator | On demand | UI link-out | Nullable; e.g., Wikidata QID |
| `description` | text | Curator | On demand | UI hover | Nullable |
| `merged_into` | FK Entity | Action `MergeEntities` | On merge | Resolver bypass | Nullable; if set, this entity is archived |

---

## 7. `Claim`

| Property | Type | Set by | Set when | Read by | Notes |
|---|---|---|---|---|---|
| `claim_id` | string (uuid) | `extract_claims` | First extraction | Everywhere | PK |
| `statement` | text | `extract_claims` | Creation | Citation UI, FlagClaim list | |
| `subject_entity_id` | FK Entity | `extract_claims` | Creation | Graph traversal | Nullable |
| `object_entity_id` | FK Entity | `extract_claims` | Creation | Graph traversal | Nullable |
| `predicate` | string | `extract_claims` | Creation | Graph display | NL predicate |
| `confidence` | float | `extract_claims` | Creation | UI threshold | 0–1 |
| `flagged` | boolean | Action `FlagClaim` | On flag | UI badge | Default false |
| `flag_reason` | text | Action `FlagClaim` | On flag | Admin UI only | Property-ACL: admin-only |
| `flagged_by` | user | Action `FlagClaim` | On flag | Admin UI | |

---

## 8. `Conversation`

| Property | Type | Set by | Set when | Read by | Notes |
|---|---|---|---|---|---|
| `conversation_id` | string (uuid) | UI / agent on first turn | Creation | Everywhere | PK |
| `owner` | user | Auto | Creation | ACL | |
| `title` | string | Agent | After first user turn (auto-summarized) | UI list | Editable |
| `corpora_scope` | array<FK Corpus> | UI | Creation; editable | `hybrid_search` filter | At least one |
| `created_at` | timestamp | Auto | Creation | UI sort | |
| `message_count` | integer | Derived | On each message | UI | |
| `last_message_at` | timestamp | Derived | On each message | UI sort | |

---

## 9. `Message`

| Property | Type | Set by | Set when | Read by | Notes |
|---|---|---|---|---|---|
| `message_id` | string (uuid) | Agent (user turn) or `compose_answer` (assistant turn) | Each turn | UI, eval | PK |
| `conversation_id` | FK Conversation | Same | Each turn | UI | |
| `role` | enum {user, assistant, system} | Same | Each turn | UI rendering | |
| `content` | text | User input or LLM | Each turn | UI | |
| `created_at` | timestamp | Auto | Creation | UI order | |
| `latency_ms` | integer | `compose_answer` (assistant only) | On creation | Telemetry | Nullable for user/system |
| `token_count` | integer | LLM response | On creation | Cost telemetry | Nullable for user |
| `tools_used` | array<string> | Agent | On creation | Eval, debug | Function names called this turn |

---

## 10. `Citation`

| Property | Type | Set by | Set when | Read by | Notes |
|---|---|---|---|---|---|
| `citation_id` | string (uuid) | `compose_answer` via Action | On answer | UI | PK |
| `message_id` | FK Message | Same | On answer | UI | |
| `chunk_id` | FK Chunk | Same | On answer | Citation UI hover | |
| `ordinal` | integer | Same | On answer | Inline `[N]` rendering | |
| `span_start` / `span_end` | integer | Same | On answer | Citation UI highlight | Char offsets within chunk text |
| `rerank_score` | float | Same | On answer | Confidence badge | |

---

## 11. `IngestionJob`

| Property | Type | Set by | Set when | Read by | Notes |
|---|---|---|---|---|---|
| `job_id` | string (uuid) | Action `IngestDocument` | On trigger | Curator dashboard | PK |
| `source_id` | FK Source | Same | On trigger | Curator UI | |
| `status` | enum {queued, running, succeeded, failed} | Transform engine | Lifecycle | Curator UI | |
| `started_at` / `finished_at` | timestamp | Same | Lifecycle | Latency calc | |
| `documents_created` / `chunks_created` | integer | Transforms | On success | Curator dashboard | |
| `error` | text | Transform | On failure | Curator UI | Nullable |
| `triggered_by_action` | string | Action engine | On trigger | Audit | Action audit ID |

---

## 12. `EvalRun`

| Property | Type | Set by | Set when | Read by | Notes |
|---|---|---|---|---|---|
| `eval_run_id` | string (uuid) | `run_eval` Function | On trigger | Admin dashboard | PK |
| `suite_name` | string | Same | On trigger | Filter | |
| `started_at` / `finished_at` | timestamp | Same | Lifecycle | Latency | |
| `metrics` | json | Same | On finish | Dashboard | recall@k, faithfulness, coverage |
| `commit_ref` | string | Same | On trigger | Provenance | Code/prompt version |
| `passed` | boolean | Same | On finish | Alerting | Suite-level pass/fail |

---

## 13. Link metadata

| Link | Properties on the link itself | Notes |
|---|---|---|
| `has_chunks` | none | Cardinality 1..n |
| `from_source` | none | n..1 |
| `belongs_to_corpus` | none | n..1 |
| `mentions` | `span_start`, `span_end`, `mention_text`, `score` | Set by `extract_entities` |
| `supports` | `score` | Set by `extract_claims` |
| `subject_of`, `object_of` | none | |
| `related_to` | `predicate: string`, `confidence: float` | Set by `extract_claims` when both subject and object resolved |
| `has_messages` | none | |
| `cites` | none | |
| `evidence_chunk` | none | |
| `produced_by` | none | |
| `scoped_to` | none | |

---

## 14. Derived / computed metadata

Some values are not written by the originating transform; they're computed by a downstream derived transform on a schedule.

| Property | Owner | Refresh | Formula |
|---|---|---|---|
| `Corpus.document_count` | Derived transform | Daily | `count(Document where corpus_id=this and status != "archived")` |
| `Conversation.message_count` | Derived transform | On insert | `count(Message where conversation_id=this)` |
| `Conversation.last_message_at` | Derived transform | On insert | `max(Message.created_at where conversation_id=this)` |
| `Source.health_score` | `compute_source_health` | Daily | `0.7 * success_rate_30d + 0.3 * latency_score` |
| `Document.summary` | `ingest_documents` summary stage | Each version | LLM-generated, ≤ 200 tokens |

---

## 15. Intent + Reasoning

- **Why catalog metadata at all.** Property-level discoverability is the precondition for property-level ACLs and property-level audit. You can't govern what you can't enumerate.
- **Why `corpus_id` is denormalized on Chunk.** Vector Search filtering is faster on a property the index already has. We pay a small consistency cost (Chunk.corpus_id must match its Document.corpus_id) for a large query-time win.
- **Why `embedding` is on Chunk, not Document.** Document-level embeddings would force averaging over heterogeneous content. Chunk-level keeps the retrieval surface as fine as the chunker.
- **Why `summary` is on Document, not Chunk.** A summary IS document-level by definition. Per-chunk summaries are an anti-pattern: they double the storage with no precision gain.
- **Why `flag_reason` is property-level ACL'd.** Moderation notes are operator-private. Other users see the badge, not the reason.
- **Why we keep raw `rerank_score` on Citations.** Eval signal. We need to retrospectively ask "did we cite low-confidence chunks more often in low-faithfulness turns?"
- **Why `tools_used` on Message.** Debug. When a turn goes wrong, you want to know exactly which tools the agent invoked without re-running.
- **Why no PII enum.** Foundry's own data-classification layer handles this; encoding it here would duplicate platform machinery and drift.

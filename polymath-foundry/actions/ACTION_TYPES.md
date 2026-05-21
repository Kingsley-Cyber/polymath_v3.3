# Action types

Every mutation to the Ontology goes through one of these. No raw upserts. No service-layer "merge first" tricks needed — Foundry actions are atomic transactions with validation, side effects, and audit trails by construction.

---

## 1. `IngestDocument`

**Purpose:** register a new source artifact for ingestion.

| | |
|---|---|
| Inputs | `source_id: string`, `corpus_id: string` |
| Validates | source exists; corpus exists; user has write on corpus |
| Effect | Append row to `/Polymath/raw/sources` → triggers `ingest_documents` transform |
| Creates | `IngestionJob` (status=queued) |
| Approval | No |

## 2. `ReingestDocument`

**Purpose:** re-ingest an existing document after a source change.

| | |
|---|---|
| Inputs | `document_id: string` |
| Validates | document exists; user has write on its corpus |
| Effect | Bump `Document.version`; delete existing Chunks; re-run transforms |
| Creates | `IngestionJob` linked to existing Document |
| Approval | No |

## 3. `MergeEntities`

**Purpose:** collapse duplicate Entity records.

| | |
|---|---|
| Inputs | `primary_entity_id: string`, `duplicate_entity_ids: array<string>` |
| Validates | all entities exist; primary is not in duplicates; user has admin role |
| Effect | Rewire `mentions`, `subject_of`, `object_of`, `related_to` links from duplicates to primary; archive duplicates with `merged_into = primary_entity_id` |
| Creates | audit record on each affected link |
| Approval | **Yes** — risky |

## 4. `FlagClaim`

**Purpose:** mark a claim as disputed / incorrect.

| | |
|---|---|
| Inputs | `claim_id: string`, `reason: string` |
| Validates | claim exists; reason ≥ 10 chars; user has flag privilege |
| Effect | Set `Claim.flagged=true`, `Claim.flag_reason=reason`, `Claim.flagged_by=user` |
| Creates | UI badge on every Message whose Citations point to Chunks supporting this Claim |
| Approval | No |

## 5. `PromoteToCanon`

**Purpose:** elevate a Document from `indexed` to `canon` status.

| | |
|---|---|
| Inputs | `document_id: string` |
| Validates | document is in status=indexed; user has curator role |
| Effect | `Document.status = canon` |
| Approval | **Yes** |

## 6. `TagDocument`

**Purpose:** add or replace tags on a Document.

| | |
|---|---|
| Inputs | `document_id: string`, `tags: array<string>`, `mode: enum {replace, append}` |
| Validates | tags are non-empty strings; ≤ 32 tags total after operation |
| Effect | Update `Document.tags` |
| Approval | No |

## 7. `SoftDeleteDocument`

**Purpose:** archive a Document without losing history.

| | |
|---|---|
| Inputs | `document_id: string`, `reason: string` |
| Validates | document exists; reason ≥ 10 chars; user has curator role |
| Effect | `Document.status = archived`; Chunks become unindexed (excluded from retrieval) but remain queryable via lineage |
| Approval | **Yes** |

---

## Cross-cutting rules

- Every Action writes an audit record (Foundry-native).
- Approval gates use Foundry's standard approval workflow — they do not need custom code.
- Actions that touch many rows (`MergeEntities`, `ReingestDocument`) run as background jobs; the UI shows progress.
- Actions are the **only** way to mutate live Ontology objects. Transforms always write to staging datasets that back the Ontology; they never bypass actions for live mutations.

---

## Internal action — used only by `compose_answer`

### `CreateMessageWithCitations`

**Purpose:** atomically write an assistant Message and its Citations.

| | |
|---|---|
| Inputs | `conversation_id: string`, `content: string`, `citations: list[{chunk_id, ordinal, span_start, span_end, rerank_score}]`, `tools_used: list[str]`, `latency_ms: int`, `token_count: int` |
| Validates | conversation exists; user owns or is shared on conversation; all chunk_ids exist and are within `conversation.corpora_scope`; at least one citation if role=assistant |
| Effect | Insert Message; insert all Citations; transaction is atomic |
| Approval | No (called only by Functions) |

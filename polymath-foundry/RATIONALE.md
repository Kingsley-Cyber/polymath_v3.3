# Rationale — why this design

The "why" for every major architectural decision in the Polymath Foundry port. If a future-you (or a teammate, or an FDE) asks "why did you do it this way?" the answer should be here.

---

## 1. Why Foundry instead of continuing on v3.3

| Operational pain on v3.3 | Foundry resolution |
|---|---|
| 6+ services in `docker-compose.yml` (Qdrant, Neo4j, Pulsar, SearXNG, Docling, FastAPI) — each with own backups, upgrades, networking | Single platform; the platform owns service ops |
| Bearer-token auth + manual user table | Foundry SSO + RBAC, native |
| Drift between Qdrant filter syntax, Neo4j Cypher, and Mongo queries | One Ontology, one query surface, one filter language |
| Custom audit logging bolted on per route | Foundry audit is platform-native |
| Schema changes require migrations across 3 stores | Single Ontology branch; review + merge |
| Eval harness lives outside the runtime; results lose context | AIP Evals; results tied to prompt + tool version |

The trade is: less control over individual primitives (you can't tune a specific Qdrant HNSW parameter), more leverage over the system as a whole.

---

## 2. Why one Ontology instead of separate vector + graph + metadata stores

v3.3 maintains three sources of truth (Qdrant, Neo4j, Mongo). Every write has to be propagated to two others; every read picks one. The cost is silent inconsistency.

The Ontology collapses this:

- Object Storage V2 holds the canonical row.
- Vector Search indexes a property of a Chunk object.
- Link types are either stored datasets or computed from FKs.
- All three share one ACL model and one audit log.

The cost: one platform vendor. We accept that.

---

## 3. Why no separate Neo4j cluster

Two reasons:

1. **The graph queries we actually need are simple.** 1–3 hop traversals (Chunk → Entity → related Entities; Message → Citation → Chunk → Document). Foundry's link types handle this. We do not need Neo4j's full graph algorithms (Louvain, PageRank, GDS) for the retrieval surface.
2. **The state divergence cost is real.** Two graph stores in v3.3 (Neo4j main + Pulsar buffer) caused most of the late-2025 ingestion incidents.

If we ever need true graph algorithms (community detection, centrality), we add a Foundry Function that runs them in-process on the Ontology — not a second cluster.

The familiar Cypher idiom is preserved in [GRAPH_SCHEMA.md](GRAPH_SCHEMA.md) for design clarity; the runtime is Foundry.

---

## 4. Why evidence-first answering

Polymath's whole reason for existing is to be more trustworthy than a stock LLM. That means every answer has to be grounded in retrievable, citable, flaggable evidence.

Concretely:

- `compose_answer` rejects LLM outputs with zero citation markers.
- Every Citation links to a specific Chunk with character-offset spans.
- The Citation links survive flagging: if a Claim that a Chunk supports is later flagged, every Citation that pointed at that Chunk surfaces a badge.

This is non-negotiable. A faster pipeline that lets uncited answers ship is a worse product.

---

## 5. Why every write is an Action

Three reasons:

1. **Audit.** Foundry Actions are atomic, audited, attributable. Direct dataset writes are not.
2. **Validation.** Action types enforce input validation centrally. The "IngestionConfig partial PATCH" bug in v3.3 (Mongo `$set` clobbering nested subdocs) was a class of mistake that doesn't exist when the only mutation path goes through a validated Action.
3. **Approval.** Risky changes (PromoteToCanon, MergeEntities, SoftDeleteDocument) require approval — implemented once at the Action layer, not 8 times across endpoints.

Transforms still write to staging datasets (raw, clean, links) but never to "live" Ontology object instances; the Ontology Manager binds the dataset to the object type, and any **mutation** to an existing object goes through an Action.

---

## 6. Why no FastAPI server

The v3.3 FastAPI server existed to:

- Serve the chat HTTP / WebSocket endpoints.
- Run auth middleware.
- Orchestrate retrieval (call Qdrant + Neo4j + reranker).
- Call OpenRouter for LLM completions.
- Stream tokens back to the React frontend.

On Foundry:

- AIP Chatbot Studio serves the chat surface (Workshop chat panel, or embedded in OSDK React).
- Foundry SSO handles auth.
- AIP Agents orchestrate retrieval via Functions.
- AIP hosts the LLMs as tools.
- AIP Chatbot streams natively.

Every responsibility maps to a platform primitive. Keeping a FastAPI process around would be carrying a server for no reason.

---

## 7. Why Functions for each retrieval stage

Five stages (`query_embed`, `expand_query`, `hybrid_search`, `rerank`, `compose_answer`) are exposed as separate Functions, not bundled into one.

- **Independent evals.** AIP Evals can target each one.
- **Independent caching.** Embed once, search twice.
- **Independent failure.** A regression in rerank doesn't require touching search.
- **Composable.** Other agents (Curator, FDE) can call `hybrid_search` directly.
- **Auditable.** `Message.tools_used` records the actual call sequence; if a turn went wrong, the path is visible.

---

## 8. Why semantic chunking, not fixed-window

A 512-token sliding window that splits mid-table or mid-function is useless to retrieval (the chunk doesn't represent a coherent unit) and worse to display in a citation (the operator sees a fragment).

Structure-first chunking (heading-aware, table-aware, code-aware) keeps the **unit of evidence** aligned with the **unit of human reading**. Length-based splitting is a secondary pass only for over-budget paragraphs.

See [INGESTION.md §3](INGESTION.md) for the full strategy.

---

## 9. Why per-document summaries

- Coarse retrieval signal (document-level rerank or filter).
- Citation UI hover preview.
- Future: document-level dedup and topic mapping.

Generated before chunking so the summarizer sees the whole document without boundary artifacts. Stored on `Document.summary`.

---

## 10. Why Entity and Claim are first-class objects (not tags)

Two operational reasons:

1. **Flagging at the right granularity.** A curator should be able to flag a single claim across an entire corpus — not flag chunks one by one, not flag whole documents. Claim-as-object makes this a single Action.
2. **Counter-evidence is a future feature.** Once Claims are queryable, "show me the contested claims about X" is a graph traversal. With inline tags, that view is hard to build.

The cost: two more object types and a couple more extraction transforms. Worth it.

---

## 11. Why AIP-hosted LLMs (and no external API keys)

- Auth and quota live in the platform — no per-developer secrets.
- Model selection is centralized and switchable without code changes.
- Output is automatically tied to the Ontology (citations, evals) without bridge code.
- No data leaves Foundry's governance boundary.

Cost: less flexibility on model choice. We accept that for v1 and revisit if eval scores demand it.

---

## 12. Why Global Branching is mandatory for Ontology and prompt changes

The blast radius of an Ontology schema change is the entire system: every Function, every Workshop view, every agent prompt that references the changed type. A bad change is hard to roll back if it's already merged.

Branching forces every such change to be reviewable in isolation, with AIP Evals as a gate. The latency cost (minutes-hours instead of seconds) is the price of not breaking the Polymath chat for an operator who is using it.

This is especially important because **AI FDE can propose Ontology edits**. Without branching, an LLM-suggested rename could land in production. With branching, every AI FDE proposal lands on a branch — review still happens.

---

## 13. Why the eval set is portable across the migration

The v3.3 eval set (queries + ground-truth chunks / answers) is the only artifact that lets us know whether the Foundry port is **better or worse** than the system it replaces. We carry it forward in raw form:

- Same queries.
- Same ground-truth chunk IDs (re-mapped to new `chunk_id` via content hash).
- Same scoring methodology.

If recall@10 in Foundry is < 0.85 (parity threshold), the port doesn't go live. The eval set is the gate.

---

## 14. Why we removed web freshness from v1 scope

v3.3's web freshness service (scheduled re-crawl of registered web Sources) added:

- A second scheduling system to monitor.
- New failure modes (drift detection false positives → unnecessary reingest).
- A dependency on SearXNG-style fetching.

For v1 on Foundry the priority is: ingestion + retrieval + chat + evidence + governance. Web freshness can be added as a scheduled transform later — the design (`Source.last_fetched_at`, ingestion idempotency on `content_sha256`) doesn't preclude it. We just don't ship it now.

---

## 15. Why we accept some Foundry-specific lock-in

The design uses Foundry-specific primitives: AIP Chatbot Studio, Foundry Vector Search, Object Storage V2, AI FDE, Global Branching. If we ever moved off Foundry, we'd need replacements for all of them.

This is a real cost. We accept it because:

- The single-operator (you) ops burden of self-hosting v3.3 has reached the point of diminishing returns.
- Foundry's primitives compose better than the hand-assembled v3.3 stack.
- The data (Documents, Chunks, Entities, Claims) is exportable as datasets at any time. The *logic* (Functions, transforms, agent prompts) would need rewriting; the *content* would not.

---

## 16. Why this PRD exists at all

A multi-system architecture migration without a written design ends up in one of two places:

1. Re-implemented in two slightly different ways, neither owning the source of truth.
2. Abandoned partway when the operator hits a hard question and can't remember the original answer.

This folder is the answer to "why did we decide X." Every PR (in Foundry's CI), every AI FDE proposal, every operator question routes back through here.

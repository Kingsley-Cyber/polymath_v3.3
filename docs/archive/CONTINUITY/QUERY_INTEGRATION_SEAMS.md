# Query Integration Seams — Narrowest Attachment Points

**Audit date:** 2026-08-02 · Companion to `QUERY_CONTROL_PLANE_REPO_AUDIT.md`

The narrowest existing seams where target capabilities attach. Preferred forms:
adapters around stable output contracts, schema extensions, validation around
current planners, normalization wrappers around existing retrievers, facades
over existing MCP tools, release metadata on existing records.

---

## Explicitly preserved anchors

| Anchor | Identity | Why it is the anchor |
|---|---|---|
| **Query entrypoint to preserve** | `POST /api/chat` → `chat_orchestrator.process_chat_request` (`backend/services/chat_orchestrator.py`) | MCP `polymath_chat_query` already delegates here; both humans and MCP converge on one pipeline |
| **Planner to extend** | `build_query_plan_v2` → `QueryPlanV2` (`backend/services/retriever/query_plan.py`) | Authoritative, deterministic, model-free; Query IR = typed extension of this object, not a sibling |
| **Typed plan overlay to extend** | `QueryPlanV1` in `models/librarian_query_plan.py` | Already typed/hashed/replayable; the IR validation gate pattern exists here to copy |
| **Vocabulary resolver to preserve** | `corpus_vocabulary_resolver` singleton (`services/retriever/vocabulary.py`) | Fanout+merge+reservation invariant is spec-locked; wrap output only |
| **Retriever interface to adapt** | `RetrieverOrchestrator.retrieve_planned` (`services/retriever/__init__.py:1401`) | All lanes, routing, fusion, rerank live inside; adapters attach at its parameter list (`librarian_plan`, `validated_librarian_plan` precedent at line ~1427) |
| **Evidence/result type to extend** | `RetrievalResult` / `SourceChunk` (`models/schemas.py`) + `RetrievalPayload` (`models/contracts.py`) | B0 contracts are the storage shape; EvidenceItem = typed view, not new type family |
| **Storage repositories to reuse** | `services/storage/qdrant_writer.py`, `services/storage/mongo_writer.py`, `services/graph/neo4j_writer.py` | No new database abstraction; lexicon search already via `search_lexicon_entries` |
| **MCP server to facade** | `polymath_mcp/server.py` + `tools.py` | 26+ tools already registered; facade = versioned contract wrapper, not a second server |
| **Ledger pattern to reuse** | `services/control_plane/ledger.py` (Mongo `ingestion_runs` + `stage_attempts`) | Ontology control plane gets a NEW ledger family with the same shape |

---

## Seam map (capability → attachment point → form)

### S1. Typed Query IR + validation gate
- **Attach at:** `chat_orchestrator._build_chat_query_plan` (producer) and the
  top of `retrieve_planned` (consumer gate).
- **Form:** new pydantic model `models/query_ir.py` that EXTENDS QueryPlanV2
  fields (temporal compilation, ontology refs, obligation list, release pins).
  Gate: `retrieve_planned` rejects a plan failing `QueryIR.model_validate` —
  exact precedent: `validated_librarian_plan = QueryPlanV1.model_validate(...)`
  already in `retrieve_planned`.
- **Invariant enforced:** "No unvalidated Query IR reaches a retriever."

### S2. VocabularyResolutionBundle
- **Attach at:** the single return statement of
  `CorpusVocabularyResolver.resolve` (wrapped before return) and consumers
  `grounded_vocabulary_lanes` / `grounded_document_route_hints`.
- **Form:** ADAPT_WITH_WRAPPER. Resolver internals untouched; cache stores the
  wrapped form (deep-copy contract already exists).
- **Risk check:** cache key excludes vectors already; wrapper adds no key inputs.

### S3. Ontology query resolution (CREATE_NEW, thin)
- **Attach at:** immediately AFTER vocabulary resolution inside
  `retrieve_planned` (the block at ~line 1692-1731), BEFORE
  `grounded_vocabulary_lanes`.
- **Inputs:** `VocabularyResolutionBundle` + read-only
  `config/ontology.yaml` + `registries/domain_registry.v1.json` +
  `registries/predicate_normalization.v1.json`.
- **Outputs:** canonical entity/predicate/class refs stamped onto the IR —
  advisory refs that shape routing only; they never become evidence.
- **Invariant enforced:** "No vocabulary expansion becomes factual evidence"
  (ontology refs inherit the same exploratory applicability tiers).

### S4. Temporal compilation record
- **Attach at:** existing `detect_temporal_intent` call site (~line 1455 in
  `retrieve_planned`); extend the diagnostics dict into a typed
  `TemporalCompilation` (clock interpretation explicit).
- **Invariant enforced:** "No temporal query runs without explicit clock
  interpretation" — fail-open detector must emit the interpretation field
  even when inactive (`active=false, interpretation=none`).

### S5. Cross-domain budget guard
- **Attach at:** `cross_domain.py` levers + `reservation_policy.py`.
- **Form:** add a per-corpus evidence-budget accounting to existing
  `corpus_reservation_bound` diagnostics. No new allocator.
- **Invariant enforced:** "No one corpus consumes the entire cross-domain
  evidence budget" — already partially enforced by reservations; extend to
  hard accounting in the fusion diagnostics.

### S6. EvidenceItem normalization
- **Attach at:** `hydrate.py` output (the moment chunks get parent text +
  provenance) — wrap into EvidenceItems for downstream verification.
- **Form:** typed view over `SourceChunk`; no field duplication, only
  references + normalized accessors.

### S7. Contradiction detection + claim verification (CREATE_NEW, thin)
- **Attach at:** `chat_orchestrator` AFTER `RetrievalResult` returns
  (contradiction pass over EvidenceItems, pre-synthesis) and AFTER the SSE
  stream completes (claim audit vs exact evidence spans).
- **Form:** pure functions; read-only over EvidenceItems; results land in
  diagnostics and answerability traces. NEVER mutate scores or re-retrieve.
- **Invariants enforced:** "No factual answer claim lacks exact evidence",
  "No known contradiction is hidden."

### S8. Release pins + registry
- **Attach at:** retriever diagnostics dict (already carries
  `VOCABULARY_RESOLVER_VERSION`, `TEMPORAL_ROUTING_VERSION`,
  `PLANNER_VERSION`); centralize into one `release_pins` sub-dict stamped on
  every trace event.
- **Form:** metadata on existing records — no new store.

### S9. Ontology control plane (proposal → approval → activation)
- **Attach at:** NEW Mongo ledger family modeled on
  `control_plane/ledger.py` (`ontology_proposals`, `ontology_stage_attempts`),
  NEW router beside `routers/control_plane.py`.
- **Activation path:** proposals modify NOTHING until approved AND pinned in a
  release registry entry; `config/ontology.yaml` itself stays frozen —
  proposals target a versioned overlay read by the S3 adapter only.
- **Invariants enforced:** "No ontology proposal becomes active
  automatically", "No production graph write bypasses release policy",
  "No frozen extraction behavior changes without change control."

### S10. MCP stable facade
- **Attach at:** `polymath_mcp/tools.py` registration point in `server.py`.
- **Form:** tool-surface manifest (name, version, input schema hash,
  deprecation state) exposed via existing `polymath_mcp_status`; existing
  tools unchanged.

---

## Explicitly avoided (and why)

| Avoided | Reason |
|---|---|
| A second semantic gateway | `semantic_gateway.py` is ingestion-side; a query gateway forks LLM-call governance |
| A second query planner | `QueryPlanV2` is authoritative by design ("deterministic planner remains authoritative") |
| A second vocabulary resolver | fanout/merge/reservation invariant duplicated = cross-corpus fairness violation |
| Duplicate Qdrant retrievers | `search_lexicon_entries` / funnel code already own collection contracts |
| Duplicate Neo4j clients | `services/graph/` owns the driver; no LLM-Cypher invariant depends on it |
| Parallel ontology files | `config/ontology.yaml` is the frozen snapshot; proposals go to an overlay ledger |
| Parallel evidence schemas | `contracts.py` B0 already ended the untyped-dict split-brain |
| New database abstraction | Mongo/Qdrant/Neo4j writers exist; Redis is NOT in the stack — do not add it |
| Rewriting frozen extraction | PATCH-FROZEN boundary; change-control rule applies |

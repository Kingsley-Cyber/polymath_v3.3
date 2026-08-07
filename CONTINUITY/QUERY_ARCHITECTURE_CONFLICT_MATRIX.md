# Query Architecture Conflict Matrix — Documented Intent vs Repository Reality

**Audit date:** 2026-08-02 · Rule: architecture documents are design intent;
the repository is the source of truth for current implementation.

Documented sources compared: the audit's target query pipeline,
`CONTINUITY/POLYMATH_ARCHITECTURE.md` (canonical 2026-07-03), and the
general target architecture naming (Query IR, knowledge plane, ontology
control plane, MCP facade).

---

| # | Documented assumption | Actual repository implementation | Agree? | Risk of following the document literally | Recommended repository-grounded resolution |
|---|---|---|---|---|---|
| 1 | A "semantic gateway" fronts query processing | `services/semantic_gateway.py` is the INGESTION-side structured-output gateway for `SemanticDigestV1` (capability routing, validation, repair, dead-letter). Query-time LLM governance lives in `services/llm.py` + `chat_orchestrator` | NO — name collision | Building a "query semantic gateway" creates a second LLM-call governance stack and splits cost/capability policy | No new gateway. Query-side gates attach as validation stages on the Query IR inside `chat_orchestrator` / `retrieve_planned` |
| 2 | `librarian_query_plan` is a design artifact to build | Already exists: `models/librarian_query_plan.py` (QueryPlanV1, `query_plan.v1`, hashed, replayable) + `retriever/librarian_planner.py` + `librarian_decomposer.py`, wired into `retrieve_planned` with `librarian_execution_policy` | YES | Rebuilding would fork the planner and lose the shadow-eval history (`test_librarian_planner_shadow.py`) | REUSE_AS_IS; extend only via its existing versioned schema |
| 3 | `query_refinement` refines queries inside the pipeline | `services/query_refinement.py` is a HyDE-style SUGGESTION service for the Graph Query UI tab (alternative phrasings, contrarian framings), cached in Mongo `query_refinements` | PARTIAL | Wiring it into the runtime pipeline adds an LLM hop to every query and breaks the deterministic-plan invariant | Keep it UI-side. Runtime refinement already exists deterministically as vocabulary translation lanes |
| 4 | `query_model_resolver` resolves query meaning | `services/query_model_resolver.py` resolves MODEL references (`pool:` / `profile:` strings) at chat time | NO — name collision | Importing it as the query-meaning resolver silently misroutes the integration | Name the new capability `ontology_resolution`, never reuse this symbol |
| 5 | Target pipeline stage "intent and outcome analysis" needs an intent classifier | Deterministic `intent_policy.py` + `search_mode.py` already classify intent WITHOUT an LLM; outcome analysis exists post-retrieval as the answerability gate | PARTIAL | Adding an LLM intent classifier breaks determinism ("same query ⇒ same retrieval mix") | Extend `intent_policy` outputs into the Query IR; move answerability atoms earlier (plan time), still deterministic |
| 6 | "Ontology and entity grounding" stage implies an ontology resolver exists | No query-time ontology resolver. `config/ontology.yaml` is consumed only by FROZEN extraction modules; `models/semantic_resolution.py` (T9.1) is ingestion-time; `registries/domain_registry.v1.json` feeds the four-lane associative routing lane only | NO | Conflating vocabulary resolution with ontology resolution would let vocabulary expansions be treated as canonical facts | CREATE_NEW thin `ontology_resolution.py` consuming the VocabularyResolutionBundle (seam S3); vocabulary vs ontology distinction preserved |
| 7 | "Temporal compilation" is a compiler | `retriever/temporal.py` is a fail-open DETECTOR (`temporal_query_routing.v1`) with hydration in `hydrate.py`; no explicit clock-interpretation artifact | PARTIAL | Rewriting it as a parser/calendar normalizer violates its documented contract ("does not parse dates, normalize a calendar") | Keep the detector; add the typed `TemporalCompilation` record with explicit clock interpretation (seam S4) |
| 8 | Knowledge plane as a new service layer | Closest existing structures: `services/librarian/` (card_builder, shelf_engine), `services/facets/` (metadata facet runtime), `models/semantic_resolution.py`, digest/summary-tree artifacts in Qdrant | PARTIAL | A new "knowledge plane" service duplicates card/facet/digest serving already owned by ingestion artifacts | No new plane: the knowledge plane = existing digest/lexicon/facet artifacts + read-only serving adapters (S3/S6) |
| 9 | Redis caching layer | No Redis anywhere. Caches: in-process `cache_util.TTLCache`, `vocabulary_cache` (epoch+TTL), Mongo-backed idempotency caches (`query_refinements`, plan cache) | NO | Adding Redis introduces a fourth store, new readiness requirements in `retrieval_readiness.py`, and operational burden for marginal gain | Do not add Redis; extend existing epoch-cache pattern if cross-process invalidation is needed |
| 10 | Ontology control plane = extend `services/control_plane/` | `control_plane/` is Control Plane V2 scoped to INGESTION (intake ledger, artifact census, query-ready certificates) | PARTIAL | Overloading it with ontology proposals mixes ingestion readiness proofs with terminology governance; certificates would lose single meaning | New ledger FAMILY (`ontology_proposals`) reusing the ledger/certificate PATTERN in a separate module (`services/ontology_control/`), router beside `routers/control_plane.py` |
| 11 | MCP facade = new MCP server | `polymath_mcp/` already runs a FastMCP sidecar with 26+ tools, key store, auth, and shared service singletons; `polymath_chat_query` delegates to the same chat orchestrator | YES (existence) | A second MCP server forks auth, transport, and singleton bootstrap | Facade = versioned tool-surface manifest + deprecation policy OVER existing tools (seam S10) |
| 12 | Evidence verification system as a retrieval re-run | Nothing verifies answers post-synthesis; `atomic_claim_anchors.py` grounds claims INTO prompts pre-synthesis; ingestion-side `corroboration_gate.py` is frozen | NO (capability absent) | Implementing verification as a retrieval re-run doubles latency and can disagree with the packet actually shown | Verification reads the SAME `RetrievalResult`/EvidenceItems the answer used (seam S7); never re-retrieves |
| 13 | Five typed contracts need extension for Query IR | `models/contracts.py` (B0) defines five STORAGE contracts (ChunkExtraction, ChunkMetadata, RetrievalPayload, GraphWriteModel, RerankerInput) with CI gate `tests/test_contracts.py` | PARTIAL | Adding a query-plan contract into contracts.py blurs the storage-only boundary of B0 | Query IR contract lives in `models/query_ir.py`; `RetrievalPayload` stays the evidence storage shape; EvidenceItem references both |
| 14 | "No LLM-generated Cypher executes" requires new guard | Already true: Neo4j query access goes through parameterized reads in `services/graph/` and `_retrieve_graph_seed_facts`; no Cypher string construction from model output found | YES | Adding a guard layer implies a risk that does not exist and invites cargo-cult filtering | Preserve as an asserted invariant + regression test asserting no model output reaches a Cypher string |
| 15 | Release pins/registry as new infrastructure | Registries exist (23 versioned JSON files, `models/registry_loader.py`) and version strings exist (`VOCABULARY_RESOLVER_VERSION`, `PLANNER_VERSION`, `TEMPORAL_ROUTING_VERSION`) but no ACTIVE release record with promotion history | PARTIAL | Building a parallel registry store duplicates the established `registries/` convention | Release registry = one new versioned JSON + a Mongo promotion-history collection following the ledger pattern |

---

## Cross-cutting conflict classes

1. **Name collisions** (rows 1, 3, 4): three documented component names map to
   existing modules with DIFFERENT responsibilities. All new code must use
   non-colliding names (`query_ir`, `ontology_resolution`, `evidence_verification`).
2. **Document-new-file vs repository-already-there** (rows 2, 11, 15): whenever
   the target architecture proposes a filename, grep first — the behavior
   frequently exists under another name.
3. **Determinism vs LLM stages** (rows 3, 5): the canonical governing
   principles (POLYMATH_ARCHITECTURE §0) forbid LLM judgment in context
   assembly; any new stage defaulting to an LLM call conflicts unless it is an
   operator-gated escalation like `librarian_decomposer` or `grounded_planner`.
4. **Frozen boundary** (row 6): `config/ontology.yaml` and all extraction
   behavior are PATCH-FROZEN; ontology evolution happens via overlay + release
   policy, never by editing the snapshot.

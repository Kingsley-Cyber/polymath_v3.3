# Repository-Grounded Query and Control-Plane Integration Audit

**Date:** 2026-08-02 · **Audit mode:** READ-ONLY (no files modified during inspection)

## Repository state record

```text
git rev-parse --show-toplevel  → /Users/king/polymath_v3.3
git rev-parse HEAD             → 86149227db703c097aad586e4a18018185e9f742
git branch --show-current      → extraction/deterministic-recall-ladder
git status --short             → 93 entries (48 modified, 45 untracked) — DIRTY
git ls-files                   → 1554 tracked files
```

The dirty tree matches the recorded freeze classification **PATCH-FROZEN**
(`CONTINUITY/EXTRACTION_FREEZE_MANIFEST_20260802.md`, patch artifact
`CONTINUITY/EXTRACTION_FREEZE_UNCOMMITTED.patch`, sha256
`eb838080dc3887bc19af37bb18aaa5c432e736c1763c9ef34cd674f61aade3cf`).
Reproducibility of any audit conclusion therefore depends on HEAD + patch.

---

## 1. Existing architecture that will be preserved

| Component | Path | Evidence |
|---|---|---|
| HTTP query entrypoint | `backend/routers/chat.py` → `chat_orchestrator.process_chat_request` | Single SSE chat path; MCP `polymath_chat_query` delegates to the SAME orchestrator |
| Deterministic query plan | `services/retriever/query_plan.py` (`QueryPlanV2`) | Model-free, preserves original query as mandatory recall lane |
| Librarian typed plan | `models/librarian_query_plan.py` (`QueryPlanV1`) + `retriever/librarian_planner.py` / `librarian_decomposer.py` | Hashed, replayable, validated; bounded LLM escalation |
| Vocabulary resolver | `services/retriever/vocabulary.py` (`CorpusVocabularyResolver`) + `vocabulary_cache.py` | Per-corpus fanout → global merge with reservations; epoch-invalidated cache |
| Retrieval orchestrator | `services/retriever/__init__.py` (`RetrieverOrchestrator.retrieve_planned`) | Spec-locked pipeline, three-tier store boundaries |
| Document routing | `tier0_router.py`, `four_lane_router.py`, `summary_tree_navigator.py` | Scope priors that never emit evidence (AGENTS.md invariant) |
| Intent policy | `intent_policy.py`, `search_mode.py` | Deliberately heuristic; determinism is a feature |
| Temporal routing | `temporal.py` (`temporal_query_routing.v1`) | Pure, fail-open; mirrors extraction-side qualified families |
| Evidence lanes / allocation | `evidence_plan.py`, `evidence_allocation.py` | Two-lane side guarantees |
| Fusion / reservation | `planned_fusion.py`, `reservation_policy.py` | Rank-only fusion; corpus reservations |
| Cross-encoder authority | `reranker.py` | Sole scoring authority (POLYMATH_ARCHITECTURE §0.4) |
| Curation v4 (shadow) | `curation.py` | Shadow-gated by `RETRIEVAL_CURATION_V4_ENABLED`, default OFF |
| Hydration / assembly | `hydrate.py`, `assembly.py`, `excerpt.py` | Mongo fetch layer only — never search (§0.3) |
| Answerability gate | `chat_orchestrator._build_retrieval_answerability_gate` | Atoms/obligations + refusal signals |
| Gap recovery | `_missing_concept_support_query`, `_repair_lane`, `gap_profile.py` | Second-pass repair already exists |
| MCP server | `polymath_mcp/server.py`, `tools.py`, `auth.py`, `key_store.py` | FastMCP sidecar, 26+ tools, shared singletons |
| Typed storage contracts | `models/contracts.py` (B0 five contracts) | The Stage-Contract rule with CI gate |
| Control Plane V2 (ingestion) | `services/control_plane/` (ledger, desired_state, reconciler, certificate) | Durable intake ledger + query-ready certificates |
| Readiness guards | `services/retrieval_readiness.py` | Qdrant + Neo4j readiness, idempotent |
| Registries | `backend/registries/` (23 versioned JSON registries) | Loader: `models/registry_loader.py` |

## 2. Existing architecture that requires extension

1. **`QueryPlanV2` → typed Query IR.** Add typed fields for temporal
   compilation, ontology grounding refs, and evidence obligations; add a
   validation gate so no unvalidated IR reaches `retrieve_planned`. The plan
   object is the IR — do not create a parallel representation.
2. **`CorpusVocabularyResolver.resolve` output → `VocabularyResolutionBundle`.**
   Wrap the returned dict in a pydantic model (adapter only; resolver internals
   untouched).
3. **`temporal.py` diagnostics → typed `TemporalCompilation`** carrying the
   explicit clock interpretation (now-relative / document-relative / absolute).
4. **Answerability gate → pre-retrieval outcome analysis.** Today outcome
   analysis is post-retrieval only; extend the same atom machinery to plan
   time without adding an LLM.
5. **`cross_domain.py` → typed cross-domain bridge obligations** (it already
   owns the levers; it lacks an obligation record in the IR).
6. **Gap recovery → typed `GapReport`** from existing `_repair_lane` diagnostics.
7. **Query trace → unified typed trace record** (plan trace, answerability
   trace, and timings already stream separately).
8. **`registries/` → active release registry** with promotion history on top
   of existing loader.
9. **`control_plane` ledger → rollback substrate** (append-only receipts
   exist; the executor does not).
10. **Selective reprocessing** already exists as census-driven repair; extend
    with version-stamp filters (`extract_schema_version`/`promote_version`
    already stamped per POLYMATH_ARCHITECTURE §1).
11. **MCP facade contract** — versioned tool surface + deprecation policy over
    the existing 26+ tools.

## 3. Genuine missing capabilities

Each `CREATE_NEW` below includes absence proof (full proofs in
`QUERY_EXISTING_CAPABILITY_MATRIX.yaml`):

1. **Ontology query resolver** — maps resolved vocabulary to canonical
   entities/predicates/classes/roles. Absence: `config/ontology.yaml` is
   consumed only by the frozen extraction modules; `models/semantic_resolution.py`
   is ingestion-time T9.1 candidates; nothing resolves query terms to canonical
   entities at query time. Constraint: consumes `VocabularyResolutionBundle`,
   never raw user text.
2. **Query-time contradiction detection** — nothing compares retrieved
   evidence items for conflict. Ingestion-side `corroboration_gate.py` assesses
   agreement at write time (frozen), not at query time.
3. **Post-synthesis claim verification** — `atomic_claim_anchors.py` grounds
   claims *into* the prompt; no pass audits the streamed answer against exact
   evidence spans afterward.
4. **Release pins** — no version-pin metadata travels with query-path
   artifacts (only resolver version strings).
5. **Proposal workflow / approval workflow** — no human-in-loop state machine;
   ingestion certificates are automatic.
6. **Semantic diff** — config changes are hash-tracked (freeze manifest) but
   never diffed semantically.
7. **Impact analysis** — no component estimates downstream effects of an
   ontology change.
8. **Ontology control plane** — `services/control_plane/` is ingestion
   Control Plane V2; an ontology proposal ledger must be new, but must reuse
   the ledger/certificate pattern, not fork it.

## 4. Proposed adapters

| Adapter | Wraps | Purpose |
|---|---|---|
| `VocabularyResolutionBundle` (pydantic) | `CorpusVocabularyResolver.resolve` dict | typed contract for downstream consumers |
| `EvidenceItem` view | `SourceChunk` + `contracts.RetrievalPayload` | normalized evidence unit for verification/contradiction stages |
| Query IR validation gate | `retrieve_planned` entry | rejects unvalidated plans before any store is touched |
| Ontology resolution adapter | `domain_registry.v1.json` + `config/ontology.yaml` (read-only) + vocabulary matches | canonical entity/predicate refs attached to the IR |
| Release-pin decorator | retriever diagnostics dict | stamps planner/resolver/reranker versions onto every trace |
| MCP stable-facade layer | `polymath_mcp/tools.py` | versioned tool contract + deprecation policy |

## 5. Proposed new modules (only when required)

| Module (proposed location) | Justification |
|---|---|
| `services/retriever/ontology_resolution.py` | capability #1 above; thin, read-only over existing registries |
| `services/retriever/evidence_verification.py` | capabilities #2 + #3; operates on EvidenceItems post-fusion and post-synthesis; read-only |
| `services/ontology_control/` (proposal ledger + approval + semantic diff) | capabilities #5–#7; reuses `control_plane` ledger/certificate patterns |
| `models/query_ir.py` | typed Query IR contract extending QueryPlanV2 fields (NOT a second planner) |

Nothing else is proposed. Specifically NOT proposed: a second semantic
gateway, a second query planner, a second vocabulary resolver, duplicate
retrievers/clients, a parallel ontology file, a new database abstraction.

## 6. Components that must not be rebuilt

- `RetrieverOrchestrator` and its funnel/lane execution — extend, never fork.
- `CorpusVocabularyResolver` — the per-corpus fanout + global merge +
  reservation invariant is spec-locked (AGENTS.md).
- `reranker.py` scoring authority — adapters may observe scores, never add
  score multipliers (POLYMATH_ARCHITECTURE §0.4).
- `polymath_mcp/server.py` sidecar lifecycle — facade only.
- `control_plane/` ingestion ledger model — ontology control plane is a NEW
  ledger family sharing the pattern, not a rewrite.
- `models/contracts.py` B0 contracts — extend fields, never fork types.

## 7. Frozen files and behaviors that must not be changed

Per `CONTINUITY/EXTRACTION_FREEZE_MANIFEST_20260802.md` (Effective Freeze
Boundary) and the Frozen Core Change-Control Rule:

```text
backend/services/extraction/*          (frames, resolver mappings, candidate
                                        construction, entity quality, scope,
                                        direction, acceptance classes, gating)
config/ontology.yaml                   (ontology snapshot)
config/relation_acceptance.yaml        (acceptance-policy snapshot)
config/predicate_synonyms.yaml
config/supported_dep_patterns.yaml
config/canonical/*
```

The complete `p2_verb_prep` boundary (passive `nsubjpass` prep_object frames,
`copular_prep` frames, and their resolver mappings) must stay co-gated.
Canonical graph writes remain disabled; no ontology proposal may auto-activate;
no change without the six-step change-control procedure.

---

## Stage 1–2 reference artifacts

- Full component inventory and call graph: `QUERY_CURRENT_CALL_GRAPH.mmd`
- Capability classification with proofs: `QUERY_EXISTING_CAPABILITY_MATRIX.yaml`
- Seams: `QUERY_INTEGRATION_SEAMS.md`
- Conflicts: `QUERY_ARCHITECTURE_CONFLICT_MATRIX.md`
- Design: `QUERY_MINIMAL_IMPLEMENTATION_PLAN.md`
- Benchmark: `QUERY_PERFORMANCE_BENCHMARK_PLAN.md`

### Key runtime facts established by inspection

1. **Two query entrypoints converge**: `POST /api/chat` and MCP
   `polymath_chat_query` both call `chat_orchestrator.process_chat_request`;
   MCP `polymath_search`/`polymath_cross_corpus_search` call
   `RetrieverOrchestrator` directly. Preserving this convergence is the seam.
2. **`semantic_gateway.py` is NOT a query gateway** — it is the ingestion-time
   structured-output gateway for `SemanticDigestV1`. Any "query semantic
   gateway" built under that name would be a fork.
3. **`query_model_resolver.py` is model selection**, not query resolution —
   name collision risk with the target architecture.
4. **`query_refinement.py` is a HyDE suggestion side-service** (Graph Query
   tab chips), not part of the runtime query pipeline.
5. **Control Plane V2 exists but is ingestion-scoped** (intake ledger,
   artifact census, query-ready certificates). It is the pattern source for
   the ontology control plane, not its implementation.
6. **Redis is absent**; caching is in-process (`cache_util.TTLCache`),
   vocabulary cache, and Mongo-backed idempotency caches.
7. **No LLM-generated Cypher anywhere**: Neo4j access at query time goes
   through `_retrieve_graph_seed_facts` / `polymath_graph_query` with
   parameterized reads — invariant currently satisfied, must be preserved.

---

## Verification record

```text
Suite: extraction frozen-core regression
Command: .venv-relex/bin/python -m pytest tests/extraction -q (from backend/)
Exit code: 0
Result: 451 passed, 0 failed, 0 xfailed, 0 skipped — matches freeze manifest
Working tree: dirty (93 entries) — PATCH-FROZEN, unchanged by this audit

Suite: query-side / control-plane / MCP (40 test modules listed in audit log)
Command: .venv-relex/bin/python -m pytest tests/test_query_plan_v2.py ... (attempted)
Exit code: collection error — ModuleNotFoundError: pydantic_settings
Baseline limitation: NOT a test failure. The backend runtime environment lives
in Docker (no containers running at audit time); the local .venv-relex is the
extraction-only environment (Python 3.11.15, spaCy stack). Query-side suites
cannot execute until the backend stack is up. Recorded, not fixed.
```

## Deterministic recommendation

```text
SAFE_WITH_PREREQUISITES
```

Prerequisites, in order:

1. **GIT-FROZEN upgrade** — commit the 93 dirty entries per the recorded
   commands (`git add backend/services/extraction backend/scripts
   backend/tests/extraction config data/closed_world_v1 CONTINUITY` +
   tag `extraction-core-v1.0.0`) so the audit baseline is reproducible by
   SHA alone.
2. **Backend environment up** — start the Docker backend stack so the 40
   query-side/control-plane/MCP test modules can execute as the pre-change
   baseline for every implementation slice.
3. **Closed-world annotation phase remains the owner-declared priority** —
   the Query IR slice 1 (contracts + adapters, zero behavior change) is
   compatible with that phase and may proceed in parallel; slices that touch
   runtime behavior should wait for a green query-side baseline.

The integration is safe because every target capability maps to an existing
seam: no parallel query stack, no duplicate retriever, no replacement of a
working service is required. The genuine new modules (ontology resolution,
evidence verification, ontology control plane) are thin, read-only, and
attach at identified seams.

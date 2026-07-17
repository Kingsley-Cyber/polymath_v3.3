# RAPTOR RAG Implementation Checklist

Last updated: 2026-07-13  
Baseline commit: `d3159b8`  

## Execution Order — SEQUENCED PLAN OF RECORD

The dependency-ordered execution plan derived from this ledger is
`docs/PLAN_CRITIQUE_2026-07-13.md` (S0–S14, with verified data-state receipts
and the critique of prior sequencing). Execute in S-order; a step's checklist
anchors are listed per row. This ledger stays the item-level source of truth;
the plan file is its ordering.

The selective semantic-architecture decision for S11–S13 is
`docs/SEMANTIC_RELATIONAL_ARCHITECTURE_DECISION_2026-07-13.md`. It replaces
theme/string/topology-based bridging as the target with a source-backed
claim/assertion → mechanism-frame → motif → validated-analogy layer. It does
not replace the existing RAPTOR hierarchy, lexicon, ERE lanes, raw child
vectors, standalone summary/tree vectors, or three-tier retrieval.

The executable extraction feasibility receipt is
`docs/SEMANTIC_EXTRACTION_PRODUCTION_READINESS_2026-07-13.md`. It confirms the
provider-neutral deterministic-first seam while blocking promotion of the
current GLiREL and joint GLiNER-Relex relation outputs. That report governs the
S11 extraction lane split and staged RunPod quality gate below.

## Standing Rules (adopted 2026-07-13 from plan critique)

1. **Capture-before-rebuild:** no summary/lexicon/card rebuild may run while an
   ADOPTED capture hook targeting the same rows is unbuilt. A rebuild must list
   the open capture hooks for its seam and obtain an explicit owner waiver,
   else the hook lands first. (Violated once — critique C1/C2; never again.)
2. **No dark fields:** every captured field ships with its first consumer, or a
   named consumer + checklist anchor in the same phase; otherwise the ledger
   must mark it dark data.
3. **Receipts refresh tags:** any status tag (IN PROGRESS/PARTIAL) contradicted
   by a later verification is stale by definition; ledger edits re-run the
   relevant verify and restamp the date.
4. **One migration per seam:** migrations touching the same artifact are
   co-scheduled (P2.5 + T-MAIN edges; S5 rollup + S7 facet cleanup payloads).
5. **Read-before-act:** agents re-read the relevant section of THIS FILE from
   disk at every phase start and before dispatching work — never from session
   memory (see repo CLAUDE.md north-star section).
Scope: ingestion, hierarchy, retrieval, librarian behavior, concept vocabulary,
GLiNER/RunPod parity, lifecycle hygiene, and all three retrieval tiers.

This file is the implementation ledger. A checkbox is complete only when the
code is deployed, durable data is reconciled where required, and the listed
acceptance tests pass. Code existing by itself is not completion.

## Status Legend

- `[x]` implemented, tested, deployed, and verified.
- `[ ]` not complete.
- **Partial** means some sub-items are complete but the parent outcome is not.
- **Experiment** means no production adoption until before/after evaluation.
- **Migration** means existing durable artifacts must be changed or rebuilt.

## Non-Negotiable Invariants

- [ ] Every retrieval change is tested through Fast (`qdrant_only`), Hybrid
  (`qdrant_mongo`), and Graph (`qdrant_mongo_graph`).
- [ ] Cross-corpus tests preserve the exact user-selected corpus boundary.
- [ ] Retrieval remains top-down: corpus/shelf -> document -> hierarchy ->
  parent/child evidence.
- [ ] The original query remains a protected retrieval lane.
- [ ] Query expansion cannot replace or semantically drift from the original
  query.
- [ ] Final evidence retains `corpus_id`, `doc_id`, parent/child identity,
  heading/boundary metadata, and source provenance.
- [ ] Summary, vocabulary, graph, and generated user-language artifacts carry
  explicit model/engine, schema version, evidence, and validation status.
- [ ] No empty-model or extractive fallback placeholder is represented as a
  valid abstractive summary.
- [ ] No query- or document-specific topic rules are hardcoded into retrieval.
  Curated aliases belong in versioned data, not Python conditionals.
- [ ] Negative controls must fail closed when the corpus lacks the answer.
- [ ] Pressure gates, provider cooldowns, durable leases, and idempotent replay
  remain active during all backfills.
- [ ] API keys and provider credentials are never printed, committed, or stored
  in plaintext artifacts.
- [ ] Embeddings and optional LLMs may generate candidates or enrich fields,
  but only deterministic evidence-backed policy may grant final document
  seats.
- [ ] An LLM may explain an already-seated path but may not add, remove, or
  protect books in the final selection.

## Current Durable Baseline

- `polymath_v2`: 84,987 summary points; 67,953 have explicit empty
  `summary_model`.
- `ecommerce_AI_FILM_SCHOOL`: 9,375 summary points; 5,796 have explicit empty
  `summary_model`.
- `markbuildsbrands_transcripts`: 657 summary points; 12 have explicit empty
  `summary_model`.
- `UGO_CORPUS`: 202 summary points; all 202 have explicit empty
  `summary_model`.
- Summary jobs: 50,522 succeeded, 500 queued, 775 superseded.
- Summary tree: 21,432 sections, 20,942 one-child sections, 23,394 rollups.
- `authentic_library`: deleted/quarantined; historical Qdrant collections still
  exist.
- Grounded planner: enabled flag set, durable budget 500, model unset; therefore
  not operational.
- Recent deployed latency probes: Fast 39.8s degraded, Hybrid 14.0s, Graph
  18.9s, cross-corpus Hybrid 23.3s.
- Reranker readiness includes real startup inference. Embedder `/health`
  currently verifies model load and stall state only; it does not certify a
  completed inference warmup.

## Audit Delta - Maintainer Response Review

The 2026-07-12 claim audit added four gaps that were understated in the
maintainer response. They are integrated below as tracked work:

- P0.3: relevance gating must cover already-selected candidates and the
  separate `ranking_policy` corpus-floor path.
- P0.6: corpus purge leases need heartbeat, graceful release, and periodic
  reclaim; startup-only recovery is insufficient.
- P0.7: the referenced follow-up acknowledgement is missing and documentation
  links require validation.
- P1.8: embedder model loading is not the same as inference warmup/readiness.

## Audit Delta 2 - 2026-07-13 Metadata Audit (owner-submitted, independently verified)

An external metadata audit was verified live on 2026-07-13 before adoption.
Confirmed: no Mongo JSON-schema validators on documents/parent_chunks/
ghost_b_extractions/corpus_lexicon/summary_tree; `extractor` absent on
0/3,000 sampled extraction rows; summary-tree `concepts` populated on
0/42,990 (polymath_v2) and 0/1,867 (ecommerce) nodes; bibliographic fields
effectively empty (author 0-1 docs, language 0, doc_profile.concepts 0);
corpus-lens facet contamination is real (68% of sampled ecommerce parents
carry `emotional_patterns`; `agentic_ai` stamped on a movement/film corpus);
parent `page_start`/entity char offsets are 0; the vocabulary-to-planner
handoff truncates positionally at 6 matches (`query_plan.py:884`) — the
mechanism that dropped a 0.909-scored expert concept from planning.
NOT confirmed (excluded until evidence): malformed schema-version strings
("polygraph/polath/polymad") — a 3,000-row sample shows only clean
v1/v2/missing; contract tests catching their own AssertionError — no such
pattern found in tests/test_contracts.py.
Discarded by design: a weighted multi-signal `final_score` formula
(conflicts with the standing "cross-encoder is the sole scoring authority /
retire multi-score fusion" decision — those signals are admissible only for
candidate generation and routing, never final-packet scoring); adopting the
full "universal evidence packet" architecture now. The later
`SEMANTIC_RELATIONAL_ARCHITECTURE_DECISION_2026-07-13.md` selectively
supersedes that blanket deferral only for the source-backed claim/assertion,
mechanism-frame, motif, and validated-analogy slice because it closes a
verified P2.5/P3.2 gap. The remaining novel layers — a broader artifact
blueprint, domain compilers, and post-synthesis usage ledger — remain deferred;
shelves/cards/sufficiency/receipts stay in their existing sections.

Tracked work added by this audit:

### P0.8 Schema Enforcement At Storage Boundaries

- [x] Add additive Mongo JSON-schema validators (warn-first, then enforce)
  for documents, parent_chunks, ghost_b_extractions, corpus_lexicon, and
  summary_tree. *(merged; validators applied warn-mode live 2026-07-13)*
- [x] Enforce typed-model acceptance at the Mongo writer boundary (close the
  B0 "writers accept ONLY typed models" gap) without breaking existing
  callers. *(T3.2: `ParentSummaryWrite` is now the only accepted command at
  the central summary writer; generation, backfill, valid repair, and
  tree-heal paths use it; focused/deployed-image suites 170 passed.)*
- [x] Normalize extraction `schema_version` (v1/v2/missing) and backfill
  `extractor` engine identity where derivable from provenance.
  *(merged; validators applied warn-mode live 2026-07-13)*
- [x] Audit graph key alignment (formal `corpus_ids` vs live node keys) and
  reconcile with a migration or a documented contract correction.

### Adopted into existing sections

- P0.5 gains: strip corpus-lens-inherited facets that lack per-document
  content evidence (measure facet DF per corpus; a lens category is not
  evidence every document teaches it), then backfill cleaned facet payloads.
  *(merged; 68,943 parents + 271 docs decontaminated with backups, leak stopped, live 2026-07-13)*
- P0.2/P2.1 gain: populate `summary_tree.concepts` at construction (the
  field exists but is never passed) and backfill from parent
  mechanisms/key_terms; persist the lexicon joins on Mongo tree rows so the
  durable hierarchy is not thinner than its Qdrant projection.
  *(merged; 28,402 nodes filled + 20,880 passthrough sections backfilled with backups, live 2026-07-13)*
- P2.1 gains: deterministic bibliographic capture/backfill (author, title,
  date, language from front matter where parseable), temporal validity
  fields (published_at / temporal_scope) on documents and cards, and a
  readiness split: operational readiness (artifacts exist, projections
  reconcile) vs metadata-quality readiness (bibliographic, facet-precision,
  card availability) as separate gates — `fully_enriched` must stop
  implying librarian-grade metadata.
- P1.2/P1.5 gain: replace the positional vocabulary-match cap in the
  planner handoff with obligation-aware selection (strong matches must be
  able to create evidence obligations regardless of list position) —
  universal cap-policy fix, never term-specific routing.
- P2.2 gains the owner-supplied "polymath librarian" latent-concept
  prompt as its generation asset (docs/LATENT_CONCEPT_PROMPT.md), still
  gated behind the P1.1 baseline + P1.7 (deterministic-first ordering
  unchanged); every harvested representation must pass the eval firewall
  and span validation.

## Temporal RAG Program — adopted 2026-07-13 with sequencing hooks

Specification of record: `TEMPORAL_RAG_E2E_IMPLEMENTATION_REPORT_2026-07-12.md`
(bitemporal, assertion-based, projection-safe; explicitly no full reingest).
Verdict: FEASIBLE and compatible with every standing invariant. Sequenced by
cross-impact so no artifact is rebuilt twice:

- [x] T-HOOK-1 (blocks the authorized corpus-scale re-extraction): extend the
  extraction wire contract with temporal CAPTURE fields (raw time expressions,
  role candidates, exact spans — capture-only, resolution stays Polymath-side)
  and redeploy the RunPod worker, so mass re-extraction runs ONCE with
  temporal capture aboard. The 1/100/500-chunk gates passed on the v2
  contract; the 5,000-chunk gate runs after this hook lands.
  *(merged + redeployed both accounts (t0nuyi6shc2t9a/t5wjsqmvpjm0lm) + live-proven 2026-07-13: v3 captures with exact offsets and cue role-candidates flow to ExtractionResult)*
- [x] T-HOOK-2 (immediate, future-only): add `temporal_class`
  (evergreen|slowly_evolving|versioned|event|ephemeral|unknown) and
  `time_expressions` to the Ghost A summary contract — the same seam that
  carries `latent_concepts`; existing rows get the deterministic classifier
  backfill in T-MAIN Phase 3, never a paid regeneration.
  *(merged + deployed + live-proven 2026-07-13: strict typed Mongo boundary,
  tagged-rescue capture, deterministic verbatim spans, and Mongo/Qdrant
  projection; UGO 203/203 explicit class + array fields, 0 span/schema errors,
  both Qdrant projections 203/203 exact, no additional model calls)*
- [x] T-HOOK-3 (merged with the P2.1 bibliographic item — one implementation):
  docling date de-conflation (publication vs file-creation vs revision),
  `source_published_at` capture, deterministic doc-date backfill.
  *(merged + deployed + live-proven 2026-07-13: all four corpora / 681 documents
  stamped through durable presence-aware backups; 86 publication-date families,
  0 mixed families, 0 file-time publication dates, 0 unexplained nulls;
  parent collections byte-identical before/after)*
- [ ] T-MAIN (after the P1.1 baseline and current retrieval work): report
  Phases 2-7 — source versions/episodes plus ONE temporal+general
  claim/assertion ledger and outbox (shared with P2.5a), Qdrant payload indexes
  + projection without re-embedding, and asserted Neo4j structure plus the
  legacy/versioned `RELATES_TO` migration (executed together with P2.5/P2.5a —
  one edge-schema migration). Keep synthesized/analogy artifacts visibly
  distinct. Add query temporal modes
  (CURRENT/AS_OF/BETWEEN/AS_KNOWN/EVOLUTION), ONE eligibility service across
  Tier-0/Fast/Hybrid/Graph, shadow-then-enforce, synthesis temporal receipts,
  and capability-specific readiness
  (`temporal_unavailable|partial|strict_ready` — same seam as the
  operational-vs-metadata-quality readiness split adopted in Audit Delta 2).
- [ ] T-QUERY-ROUTING-V1: dark-ship a default-OFF query-side temporal-intent
  detector that reuses the qualified server extraction families, preserves
  existing Qdrant/Mongo temporal carriers through both retrieval paths, and
  deterministically reserves relevant temporal/graph-supported evidence.
  Validate the immutable 24-query temporal diagnostic at >=0.90 document hit
  and >=0.70 full-anchor coverage with no frozen-suite regression and no
  corpus writes. Branch: `codex/temporal-query-routing-20260716` (review ready;
  acceptance remains open after repeated MLX embedding outages invalidated the
  frozen paired A/B; receipt: `docs/TEMPORAL_QUERY_ROUTING_AB_2026-07-16.md`).
  - [x] T-QUERY-ROUTING-V1-RERUN: on
    `codex/temporal-regression-20260717`, integrate the independently verified
    MLX evaluation-stability dependency, then run exactly one preflight-gated
    paired OFF/ON frozen 17-query × 3-tier regression under the immutable
    preregistration, runner, corpus, and MiniMax model contract. Require
    technical/corpus/citation gates plus direct >=0.85, lay >=0.75, and no
    relationship regression; preserve the existing negative verdict honestly.
    No corpus writes, no temporal-logic or scorer changes, default OFF, locked
    serial measurement, and canonical runtime restoration are mandatory.
    *(verified 2026-07-17: both arms 51/51 technical; direct 1.00→1.00,
    lay 1.00→1.00, relationship 0.75→0.75, corpus/citation/effective-tier
    1.00→1.00; the separate negative gate remained RED at 0.5556→0.4444 and
    is reported without concealment. Receipt:
    `docs/TEMPORAL_QUERY_ROUTING_FROZEN_REGRESSION_2026-07-17.md`.)*
- Ordering rationale: field capture rides in-flight generation/backfills for
  free; retrieval-behavior changes are gated by the held-out suite; temporal
  eligibility needs the fields to exist first; re-extracting before the
  contract hook would force a second paid extraction pass.

## Semantic-Relational Architecture Decision — Updated 5Ws

Decision of record:
`docs/SEMANTIC_RELATIONAL_ARCHITECTURE_DECISION_2026-07-13.md`.

- [ ] CLAIM-ANCHOR-ADDITIVITY-V2: rebase the sentence-to-chunk claim join onto
  the current retrieval path, attach anchors only after final source
  selection, and prove byte-exact source identity plus non-anchor evidence
  with the six-query replay. Clean malformed compiler claim text only while
  rendering answer context; preserve stored compiler output unchanged. Keep
  the feature default-OFF pending the serialized live validation gate
  (18/18 structural anchors and at least two rendered anchors for q021).

- **Who:** extraction/semantic-contract, graph, librarian/retrieval, and
  evaluation owners; users asking cross-domain synthesis questions. The answer
  LLM explains accepted paths but never upgrades their inference status.
- **What:** add one versioned Claim/Assertion → FrameInstance/RoleBinding →
  Motif → validated Analogy layer above the existing ERE, lexicon, hierarchy,
  and cards, plus a controlled domain registry that remains orthogonal to the
  mechanism layer. Replace only the future bridge-inference slice, not the
  retrieval architecture.
- **When:** after a dedicated labeled cross-domain slice and trustworthy exact
  spans; capture lands before the PoC pair's mass re-extraction, then canary and
  annotate-only gates precede graph/retrieval promotion.
- **Where:** Mongo is authoritative for full semantic artifacts and evidence;
  Neo4j holds bounded rebuildable structure; Qdrant generates candidates;
  `corpus_lexicon` remains the concept/sense authority; the current summary
  tree remains the hierarchy authority.
- **Why:** current triples, exact-string mechanism overlap, embeddings, and
  graph topology can nominate candidates but cannot preserve assertion scope,
  modality, conditions, causal roles, analogy invariants, or break conditions.
  The added layer makes transfer auditable without discarding working recall
  lanes.

The implementation schema, registry, prompts, storage names, Cypher, and
migration mechanics are intentionally deferred to the owner-approved `HOW`
phase. Fixed 18-theme routing, generic summary-prepended child vectors, the
attachment's frame weights/thresholds, and an RDF/SHACL runtime are not adopted
as defaults.

## Governing Librarian Planning Constraint

The librarian must ship from trustworthy existing projections before any
generative card enrichment becomes a dependency:

```text
v1: seat only what indexed overlap + deterministic rules can defend
v2: optional LLM enriches fields, retrieves candidates, explains paths, and
    fills evidence-backed gaps
never: an LLM decides which documents receive final seats
```

Deterministic seat eligibility is query-relative. Cards store universal facts;
they do not permanently label a document Direct, Foundational, Adjacent,
Bridge, or Counterbalance. A document may satisfy multiple roles and multiple
themes for the same query, but it appears once in the final packet with every
validated reason attached.

Hard rule for Bridge seats:

```text
query goal/capability IDs
  -> shared transferable_principle or mechanism IDs
  -> candidate document
  -> evidence IDs/spans in both the query-side and candidate-side sources
```

If the chain cannot be resolved from indexed IDs and source evidence, the
candidate is not granted a Bridge seat. Dense similarity alone is candidate
generation, never bridge proof.

## Deterministic-First Librarian Build Order

### Phase 0 - Trustworthy Catalog

- [ ] Complete P0.1 summary integrity and remove retrieval-eligible
  placeholders.
- [ ] Complete P0.6 ownership/orphan cleanup for active and deleted corpora.
- [ ] Backfill/reconcile `polymath_v2` concepts, mechanisms, semantic types,
  source bindings, and promotion/readiness truth from existing artifacts.
- [ ] Keep corpus readiness as the publication gate for librarian cards.

Milestone acceptance:

- [ ] Active corpora are physically owned and clean.
- [ ] Promote/lexicon/mechanism coverage is measured and not hollow.
- [ ] No librarian card is published from placeholder or failed projections.

### Phase 1 - Deterministic `librarian_card.v0`

- [ ] Decide + implement the card-production contract for NEW ingests:
  either ingestion auto-produces cards as a final deterministic pass, or the
  post-ingest builder step is a documented required stage (adopted 2026-07-14
  from smoke-gate g6: fresh corpus had lexicon but no cards until the builder
  ran).
- [x] Build cards from existing lexicon, Ghost B, summary semantics, document
  profiles, tree bindings, and promotion artifacts only.
  *(merged + built 2026-07-13: 673 cards live — v2 495, mark 101, ecom 76, UGO 1; per-field provenance; zero-seed docs skipped, never fabricated)*
- [x] Write the authoritative card to Mongo and a slim routing projection to
  Tier-0/Qdrant. (Mongo `librarian_cards` upsert + returned slim payload
  *(merged + built 2026-07-13: 673 cards live — v2 495, mark 101, ecom 76, UGO 1; per-field provenance; zero-seed docs skipped, never fabricated)*; the Qdrant/Tier-0 write is
  deliberately NOT in wave 1.)
- [x] Leave unsupported fields empty; do not infer prose to make cards look
  complete. *(merged + built 2026-07-13: 673 cards live — v2 495, mark 101, ecom 76, UGO 1; per-field provenance; zero-seed docs skipped, never fabricated)*
- [x] Normalize every value through corpus lexicon `canonical_key` identity.
  *(merged + built 2026-07-13: 673 cards live — v2 495, mark 101, ecom 76, UGO 1; per-field provenance; zero-seed docs skipped, never fabricated)*
- [x] Reject every card value without source IDs/spans and derivation method.
  *(merged + built 2026-07-13: 673 cards live — v2 495, mark 101, ecom 76, UGO 1; per-field provenance; zero-seed docs skipped, never fabricated)*

Milestone acceptance:

- [ ] The same card schema works across every corpus and source type.
- [ ] Cards remain usable when all optional LLM providers are disabled.
- [ ] Missing cards degrade to the existing Tier-0 document path.

### Phase 2 - Deterministic Query And Seat Policy

- [ ] Resolve story/query language to existing capability/concept IDs through
  lexicon/vocabulary first.
- [ ] Generate candidate documents through current Tier-0 dense/sparse recall.
- [x] Assign query-relative shelf roles through indexed field overlap.
  *(merged + live-probed 2026-07-13 on the PoC pair: 3 direct / 2 adjacent / 2 bridges with evidence chains / policy-triggered counterbalance; foundational honestly skipped)*
- [ ] Implement `shelf_reserve` through the calibrated eligibility discipline
  required by P0.3; never mirror the old unconditional corpus-floor behavior.
  **[IN CODE — merged DARK on main @f049041 (2026-07-13),
  SHELF_RESERVE_ENABLED=False; tests/test_shelf_reserve_wiring.py 16 passed
  (exit 0) in the deployed backend container post-merge; flip via S8
  before/after evals only]**
- [ ] Descend reserved documents through tree -> parent -> child evidence.
- [ ] Require shared mechanisms/principles plus evidence for every Bridge seat.
- [x] Use versioned misuse/counterbalance policy data, not per-corpus Python
  conditionals. *(merged + live-probed 2026-07-13 on the PoC pair: 3 direct / 2 adjacent / 2 bridges with evidence chains / policy-triggered counterbalance; foundational honestly skipped)*

Milestone acceptance:

- [ ] Growth-story queries do not return only near-duplicate Direct books when
  eligible Foundational, Adjacent, Bridge, or Counterbalance candidates exist.
- [ ] Every seat is explainable from card fields and evidence IDs.
- [ ] Empty or weak card coverage safely falls back to today's retrieval.

### Phase 3 - Deterministic Hardening

- [ ] Move remaining curated aliases/rules into versioned lexicon/policy data.
- [ ] Complete P1.7 vocabulary batching and caching.
- [ ] After the P1.1 baseline and P1.7 latency prerequisites exist, materialize
  P2.1 chunk-derived associations, usage frames, specificity statistics, and
  deterministic multi-point concept representations.
- [ ] Retain the existing pure evidence-join `transfer_edge` nomination
  (shared principle/mechanism across documents with different central subjects)
  only as the P3.2 baseline; an accepted semantic analogy additionally requires
  validated claims, frame/role alignment, direction, invariant, break
  conditions, and two-sided exact evidence.
- [ ] Surface shelf, matched fields, evidence IDs, and rejected seat reasons
  directly from deterministic diagnostics.
- [ ] Measure librarian behavior with P1.1 before enabling optional LLM stages.

### Phase 4 - Optional LLM Amplification

- [ ] Enrich only missing card fields such as problems, risks, and situations,
  with span/source validation and explicit generated provenance.
- [ ] Use the grounded planner only when deterministic story-to-capability
  mapping is thin.
- [ ] Let the LLM explain seated paths and caveats from card fields without
  changing final seats.
- [ ] Add only the generated P2.2 representation methods after deterministic
  multi-point recall, storage, latency, and librarian behavior are measured.

Milestone acceptance:

- [ ] Given the same candidate pool, indexed artifacts, and policy version, the
  deterministic seat engine returns the same seats regardless of whether an
  LLM participated upstream.
- [ ] An LLM may expand the candidate pool, but every newly reachable candidate
  passes the identical deterministic field/evidence gates and records
  `candidate_source`, cited concept IDs, and rejected/accepted reasons.
- [ ] Final seat selection can be replayed deterministically from the
  materialized candidate pool and policy version without another LLM call.
- [ ] LLM use improves recall/explanation quality without becoming selection
  authority.

## P0 - Summary Integrity

### P0.1 Stop Placeholder Summaries

- [x] Exclude explicit empty-model summary points from Funnel A.
- [x] Refuse new summary writes without explicit model provenance.
- [x] Count only summaries actually accepted by the Qdrant writer.
- [x] Reconcile the 500 queued summary jobs against current artifacts.
  *(2026-07-13: all 500 belonged to the deleted `authentic_library`;
  superseded with reason `corpus_not_active_orphan_job`, backed up, 0 queued
  remain — commit 1171e9d.)*
- [x] Backfill valid summaries for every summary-required parent.
  *(2026-07-13 restamp: `p0_1_summary_integrity.py verify` PASSES all four
  corpora — polymath_v2 86,880 + ecommerce 9,453 + markbuilds 1,009 + UGO 203
  all attributed, 0 empty-model. NOTE (plan critique C1): this pass ran BEFORE
  T-HOOK-2/latent capture existed, so pair rows are back-level on capture
  fields — the S4 consolidated pass carries them.)*
- [x] Remove or supersede legacy empty-model summary points after backfill.
  *(2026-07-13 restamp: verify green, 0 empty-model summaries remain on any
  corpus; prior in-progress reprojection completed.)*
- [ ] Verify every valid summary has child IDs, boundaries, model, schema,
  validation status, and evidence-backed semantics.
- [ ] Verify document-summary trees roll up only validated children.

Acceptance:

- [x] `explicit_empty_model == 0` among retrieval-eligible summary points.
  *(verified 2026-07-13: 0 across all four corpora)*
- [x] Summary-required coverage is 100% for every strict-ready corpus.
  *(verified 2026-07-13: 86,880/86,880 + 9,453 + 1,009 + 203, all attributed)*
- [ ] Funnel A returns no byte-identical parent replacement presented as a
  summary.
- [ ] Fast, Hybrid, and Graph recall do not regress after placeholder removal.

### P0.2 Repair Degenerate Hierarchy

- [x] Treat parser-emitted `Page N` headings as non-semantic structure.
- [x] Add deterministic singleton section passthrough IDs.
- [x] Skip duplicate rollup search when a new section has one passthrough child.
- [x] Backfill passthrough payloads for existing one-child section points.
  *(merged; 28,402 nodes filled + 20,880 passthrough sections backfilled with backups, live 2026-07-13)*
- [ ] Decide whether future singleton sections should be physically omitted or
  retained as aliases for stable IDs.
- [ ] Measure section/rollup storage and query round trips after migration.
- [ ] Validate document profile, section, rollup, parent, and child boundaries
  on PDF, transcript, Markdown, HTML, and structured-list fixtures.

Acceptance:

- [ ] Existing trees use the passthrough path without re-embedding.
- [ ] One-child descent performs no redundant vector search.
- [ ] Broad retrieval still reaches every parent formerly reachable through the
  duplicate section.
- [ ] New trees do not fragment at page-number headings.

## P0 - Retrieval Correctness

### P0.3 Finish Corpus-Floor Calibration

- [x] Add bounded relevance to late planned-fusion corpus reservations.
- [x] Apply the same relevance gate before an already-selected corpus candidate
  is protected as a reservation. *(live 2026-07-13: cross-corpus packet seats evidence, reasons in diagnostics)*
- [x] Trace `ranking_policy` corpus-floor eligibility on calibrated packet
  scores rather than only normalized MMR relevance.
  *(live 2026-07-13: cross-corpus packet seats evidence, reasons in diagnostics)*
- [x] Consolidate or explicitly order the `planned_fusion` and `ranking_policy`
  corpus-floor decisions so one path cannot undo the other's rejection.
  *(live 2026-07-13)*
- [x] Remove or justify the unconditional `+0.10` reserve bonus.
  *(live 2026-07-13)*
- [x] Require diagnostics to distinguish naturally selected corpus evidence
  from quota-reserved evidence. *(live 2026-07-13)*
- [x] Add a test where one selected corpus has no relevant evidence.
  *(tests/test_corpus_floor_calibration.py, green in container)*
- [x] Add a test where all selected corpora genuinely contribute.
  *(tests/test_corpus_floor_calibration.py, green in container)*

Acceptance:

- [x] No sub-threshold corpus receives a forced final seat.
  *(live 2026-07-13: q046-class cross-corpus answers with evidence from both corpora)*
- [ ] Relevant cross-corpus questions retain evidence from each necessary
  corpus.
- [x] `corpus_floor.skipped` reports why a selected shelf was omitted.
  *(live 2026-07-13)*

### P0.4 Make Answerability Honest

- [x] Recalibrate the chat-side answerability arbiter against the frozen
  9-execution negative subset without changing retriever sufficiency. Keep the
  tuning versioned/default-OFF; refuse when decisive query entities are wholly
  absent from the selected corpus, while preserving direct document-hit >=85%
  and lay-language document-hit >=75%. Require per-query OFF/ON receipts and
  a read-only corpus census. **[OWNER-DIRECTED 2026-07-16; branch
  `codex/refusal-arbiter-20260716`; acceptance green; receipt:
  `docs/ANSWERABILITY_CORPUS_SCOPE_V2_AB_2026-07-17.md`]**
- [x] Chat-facing negative control currently fails closed for the tungsten
  query.
- [x] Rename or clearly separate lane coverage from answerability in every
  diagnostic contract. *(live 2026-07-13)*
- [x] Calibrate evidence sufficiency by query/answer shape, not one universal
  threshold. *(live 2026-07-13)*
- [ ] Require answer presence, evidence strength, obligation coverage, and
  contradiction checks before `answerable=true`. **[PARTIAL @f2fb6e2 —
  undecomposed queries now judged by the strict evidence-atom gate instead of
  synthetic-lane coverage. REMAINING (verified absent 2026-07-13, S0): no
  contradiction check exists anywhere in the gate/sufficiency path (zero
  code hits in the chat gate, retriever sufficiency, answerability_tuning)
  and no asserting test covers one (zero contradiction assertions across
  test_answerability_honesty/_gate_loosening/_text_fallback); deliberately
  NOT built in S0 — needs its own design + asserting test before this box
  can flip]**
- [x] Surface a precise refusal reason when sources cover a nearby but different
  concept. *(live 2026-07-13: refusal names real concepts + nearest documents)*

Acceptance:

- [x] Negative controls across all three tiers return `answerable=false`.
  *(live 2026-07-13: tungsten fails closed on probe)*
- [x] Strong answers are not rejected solely because calibrated score ranges
  differ by query type. *(live 2026-07-13: q046 answers; residual generic-atom calibration tracked in redesign Phase 2)*
- [x] Lane coverage and answerability are separately visible in UI/MCP output.
  *(live 2026-07-13)*

### P0.5 Complete Chunk And Metadata Hygiene

- [x] Drop separator-only child chunks during ingestion.
- [x] Filter separator-only candidates before reranking.
- [x] Gate broad one-word hand-authored facet aliases by specificity.
- [ ] Audit OCR-corrupt heading paths and define a source-safe repair rule.
  **[IN CODE — evidence gathered 2026-07-13, awaiting owner sign-off:
  docs/DISPOSITION_MATRIX_2026-07-13.md — full census ecom/mark/UGO + v2
  sample; ecom 7.6% corrupt-flagged parents across 9 docs, 45.2% page-slug
  headings; proposed deterministic repair rule in §7 of the matrix]**
- [ ] Backfill missing `doc_name`, filename, and source identity on legacy
  payloads.
- [ ] Move remaining curated content aliases from Python into a versioned
  lexicon dataset.
- [ ] Add artifact diagnostics for facet source, confidence, and evidence.

Acceptance:

- [ ] No separator/navigation artifact can enter a final evidence packet.
- [ ] No broad incidental word stamps unrelated domain facets.
- [ ] Every final source has a usable document label and source boundary.

## P0 - Lifecycle And Storage Hygiene

### P0.6 Corpus Deletion And Orphan Cleanup

- [x] Delete shared Tier-0 document cards for future corpus deletion.
- [x] Retire summary-tree rows and durable source/document/extraction/summary/
  graph jobs.
- [x] Persist corpus-cleanup ownership/lease fields and hold strong references
  to active bulk-deletion tasks.
- [x] Add periodic reclaim of expired and partial cleanup leases during normal
  uptime; do not rely only on service startup. *(live 2026-07-13)*
- [x] Heartbeat/extend the lease while a large chunk or Neo4j purge is active.
  *(live 2026-07-13)*
- [x] Release or shorten the lease when graceful shutdown cancels a cleanup
  task, allowing the replacement process to reclaim it immediately.
  *(live 2026-07-13)*
- [x] Retry partial cleanup automatically after `cleanup_retry_at` without
  requiring another process restart. *(live 2026-07-13)*
- [ ] Test immediate restart before lease expiry, purge duration beyond lease,
  two competing service processes, partial-stage retry, and idempotent replay.
  **[PARTIAL @0dc5a7e — owner-guarded finalize, shutdown release, heartbeat,
  reclaim, partial-retry covered (17 green); restart-before-expiry and
  purge-beyond-lease scenarios still to add]**
- [x] Produce a dry-run ownership manifest for historical Qdrant collections,
  Tier-0 cards, tree rows, and Mongo records.
  *(2026-07-13 `orphan_ownership_manifest.py` → docs/baselines/
  ORPHAN_MANIFEST_2026-07-13.json: 17 dead corpus ids; authentic_library
  residue 1,701,144 Mongo rows + 638,743 Neo4j nodes — commit 0dc5a7e.)*
- [x] Review and approve the exact deletion allow-list. *(owner approved 2026-07-13: "purge approved")*
- [x] Execute the one-time orphan cleanup. *(2026-07-13: manifest-driven; re-verified 0 dead ids / 0 orphan collections / 0 residue)*
- [x] Verify deleted `authentic_library` projections are removed or explicitly
  retained with a documented reason. *(2026-07-13: fully purged — 1.7M Mongo rows, 638k Neo4j nodes, collections dropped)*
- [ ] Add scheduled ownership reconciliation with report-only default.

Acceptance:

- [ ] No active retrieval route references a deleted/nonexistent corpus.
- [ ] No approved orphan collection remains after cleanup.
- [ ] Re-running cleanup is idempotent and does not affect active corpora.
- [ ] An interrupted purge reaches complete/partial terminal truth without an
  operator-triggered second restart.

### P0.7 Documentation And Claim Integrity

- [x] Create the referenced
  `RAPTOR_CRITIQUE_ACKNOWLEDGEMENT_2026-07-12.md` or remove the broken link from
  the maintainer response.
- [x] Add an automated relative-link check for tracked Markdown files
  (`backend/scripts/check_markdown_links.py`, non-zero exit on failure).
- [x] Require completion reports to distinguish deployed code, migrated legacy
  data, and future-only behavior (standing convention recorded in the
  acknowledgement; the Implementation Log entries follow it, including
  explicit deploy-pending status).
- [x] Require health/readiness claims to identify whether they test process
  liveness, model load, or real inference (convention recorded in the
  acknowledgement; the endpoint-level separation itself is tracked as P1.8).
- [x] Keep durable baseline counts and probe timestamps attached to claims that
  may become stale (`docs/baselines/` census + latency artifacts, referenced
  from Implementation Log entries).

Acceptance:

- [x] Every referenced local document exists and resolves (link check green
  across 72 tracked files, 2026-07-13).
- [x] No repair is called complete while its legacy data migration remains
  open (Implementation Log discipline; P0.1 boxes stay open until every
  corpus verifies).
- [x] Runtime claims can be reproduced by a credential-free command
  (`capture_raptor_baseline.py`, `probe_tier_latency.py`,
  `p0_1_summary_integrity.py verify` — secrets stay in `.env`/container).

## P1 - Librarian Query Understanding

### P1.1 Establish The Evaluation Set First

- [x] Create at least 50 held-out lay-language questions across film,
  psychology, ecommerce, marketing, philosophy, and technical material.
  *(56 questions in `backend/evals/heldout_questions.jsonl`, all four active
  corpora, Mongo-validated expected docs — commit f2fb6e2.)*
- [x] Include direct, naive-vocabulary, broad, list, comparison, procedural,
  follow-up, negative-control, cross-domain, and cross-corpus cases.
  *(shape census: direct 8, naive 6, single-fact 5, broad 4, list 6,
  procedural 6, comparison 3, follow-up 3, negative-control 5, cross-domain
  2, cross-corpus 6, cross-corpus-irrelevant 2.)*
- [x] Record expected documents, concepts, evidence, and acceptable alternate
  routes. *(expected_doc_ids validated against Mongo per question, expected
  concepts, expected_all_docs semantics, alternate-route notes.)*
- [x] Capture baseline Recall@K, document recall, concept recall, nDCG/MRR,
  answerability, evidence coverage, diversity, and latency by tier.
  *(captured 2026-07-13 post-P0.1/restart, scorer v3, 0 errors:
  Fast 84.3% doc-hit / 39.7s; Hybrid 90.2% / 55.5s; Graph 94.3% / 62.3s
  with 5/5 negative controls fail-closed on the corrected set;
  docs/baselines/EVAL_2026-07-13_*.json)*
- [ ] Add a librarian rubric: direct relevance, useful adjacency, bridge
  validity, counterbalance, provenance, and harmful analogy penalty.
- [x] Store stable hashes for every held-out query and exclude those hashes,
  expected answers, and evaluator-authored paraphrases from attested-query
  harvesting and any training/backfill source.
  *(56 frozen sha256 hashes in `backend/evals/heldout_hashes.json`;
  `services/eval_firewall.is_heldout_query()` is the mandatory gate for any
  future harvesting — commit f2fb6e2.)*
- [ ] Add an automated contamination check that fails an evaluation run when a
  held-out query appears in attested/generated concept representations.
  **[firewall primitive exists; the eval-run check lands with the first
  attested/generated representation store (P2.2)]**

Acceptance:

- [ ] No ranking, expansion, thematic-routing, or concept-vector change ships without a
  before/after result on this suite.

### P1.2 Activate The Grounded Planner Safely

- [x] Constrained grounded planner and durable cache exist.
- [x] Selective trigger logic exists.
- [ ] Block production activation until deterministic `librarian_card.v0`,
  shelf scoring, seat policy, and the held-out baseline are operational.
- [ ] Configure an explicit planner provider/model and validate the durable call
  budget.
- [ ] Trigger only when deterministic vocabulary/plan confidence is weak or the
  query requires grounded cross-domain expansion.
- [ ] Preserve original-query and exact-string lanes.
- [ ] Reject introduced terms without selected-corpus lexicon IDs.
- [ ] Log provider calls, cache hits, cited concept IDs, rejected expansions,
  latency, and token usage.

Acceptance:

- [ ] Lay-query concept/document recall improves materially over baseline.
- [ ] Direct expert queries do not regress.
- [ ] Planner drift cannot satisfy a required lane without source evidence.

### P1.3 Add Conversation And Open-Book Anchoring

- [ ] Distinguish session context from persistent user preferences.
- [ ] Allow users to inspect, edit, or disable persistent profile signals.
- [ ] Pass accepted document/source anchors into follow-up retrieval.
- [ ] Use anchors as ranking priors, never absolute filters.
- [ ] Preserve room for adjacent/counterbalancing sources.
- [ ] Test pronouns, “what else does this author say?”, and follow-up comparison.

Acceptance:

- [ ] Follow-ups retain the open document while still finding necessary
  external evidence.
- [ ] Sensitive personal traits are never silently inferred or persisted.

### P1.4 Route Shelves Before Books

- [ ] Define a versioned corpus/shelf profile card derived from document
  profiles, concepts, provenance, freshness, and readiness.
- [ ] Index corpus cards with distinct `kind` and corpus ownership.
- [ ] Route unscoped queries to high-recall candidate shelves before document
  routing.
- [ ] Preserve user-selected corpora even when shelf routing is weak.
- [ ] Add fallback fan-out when shelf confidence is ambiguous.
- [ ] Surface the shelves searched and skipped.

Acceptance:

- [ ] Unscoped queries reduce unnecessary corpus fan-out without recall loss.
- [ ] Cross-domain questions still discover non-obvious but defensible shelves.

### P1.5 Implement Librarian Selection Roles

- [x] Add retrieval roles: direct, foundational, adjacent, bridge, and
  counterbalance. *(merged + live-probed 2026-07-13 on the PoC pair: 3 direct / 2 adjacent / 2 bridges with evidence chains / policy-triggered counterbalance; foundational honestly skipped)*
- [x] Assign roles per query; never stamp a document with one permanent shelf
  role. *(merged + live-probed 2026-07-13 on the PoC pair: 3 direct / 2 adjacent / 2 bridges with evidence chains / policy-triggered counterbalance; foundational honestly skipped)*
- [ ] Derive roles from universal capabilities, mechanisms, problems, risks,
  and source evidence rather than topic-specific rules.
- [x] Define deterministic v0 eligibility:
  *(merged + live-probed 2026-07-13 on the PoC pair: 3 direct / 2 adjacent / 2 bridges with evidence chains / policy-triggered counterbalance; foundational honestly skipped)*
  - Direct: central-subject/problem overlap with the query.
  - Foundational: capability overlap supported by mechanisms/evidence.
  - Adjacent: shared capabilities/mechanisms with meaningfully different
    central subjects.
  - Bridge: shared transferable-principle/mechanism IDs, different subjects,
    and source evidence on both sides.
  - Counterbalance: a versioned policy trigger plus an evidence-backed
    counterbalancing concept/document.
- [x] Require every bridge recommendation to expose:
  `document -> concept -> transferable principle -> user goal`.
  *(merged + live-probed 2026-07-13 on the PoC pair: 3 direct / 2 adjacent / 2 bridges with evidence chains / policy-triggered counterbalance; foundational honestly skipped)*
- [x] Treat embedding scores as candidate recall only; they cannot independently
  satisfy any shelf-role gate. *(merged + live-probed 2026-07-13 on the PoC pair: 3 direct / 2 adjacent / 2 bridges with evidence chains / policy-triggered counterbalance; foundational honestly skipped)*
- [x] Deduplicate by document while retaining multiple validated roles, themes,
  and reasons. *(merged + live-probed 2026-07-13 on the PoC pair: 3 direct / 2 adjacent / 2 bridges with evidence chains / policy-triggered counterbalance; foundational honestly skipped)*
- [ ] Reserve role diversity only when candidates clear relevance/evidence
  gates. **[IN CODE — wave3/reserve, dark behind SHELF_RESERVE_ENABLED,
  pending before/after evals]**
- [x] Skip a role seat when no candidate qualifies; never fill a quota with
  weak evidence. *(merged + live-probed 2026-07-13 on the PoC pair: 3 direct / 2 adjacent / 2 bridges with evidence chains / policy-triggered counterbalance; foundational honestly skipped)*
- [ ] Return a reading path, not merely a flat chunk list, for librarian-mode
  questions. **[IN CODE — wave3/reserve (diagnostics seed:
  meta.shelf_reserve.reading_path), dark behind SHELF_RESERVE_ENABLED,
  pending before/after evals]**

Acceptance:

- [ ] Non-obvious recommendations are explainable and source-grounded.
- [ ] Serendipity does not become random semantic association.
- [ ] Counterbalancing sources are included for manipulation, bias, or safety
  risks where relevant.

### P1.6 Route By Answer Shape

- [ ] Formalize definition, procedure, enumeration, comparison, relationship,
  broad synthesis, recommendation, and decision-support policies.
- [x] Dark-ship the existing per-side, distinct-document evidence allocator
  behind a settings flag that defaults OFF and is eligible only for the shared
  relationship/comparison detector. Require the original allocator assertions,
  adjacent detector regressions, and a frozen three-tier OFF/ON evaluation with
  relationship minimum-distinct >=75% and no direct/lay-language regression
  before any production activation. **[COMPLETE 2026-07-16; receipt:
  `docs/RELATIONSHIP_EVIDENCE_ALLOCATION_AB_2026-07-16.md`; default remains OFF]**
- [ ] Allow validated summaries/themes as synthesis scaffolds for broad queries.
- [ ] Hydrate child/parent evidence before treating a generated summary as a
  citation.
- [ ] Use sibling expansion for structured list/enumeration nodes.
- [ ] Use dynamic breadth/K based on score cliffs, obligations, and answer
  shape.

Acceptance:

- [ ] List questions retrieve complete sibling sets when source structure
  supports them.
- [ ] Broad questions gain document diversity without sacrificing evidence.

## P1 - Latency And Resource Isolation

### P1.7 Batch And Cache Vocabulary Work

- [ ] Gather all plan embedding texts before vocabulary/tree/funnel fan-out.
- [ ] Reuse vectors across planner, vocabulary, tree, and retrieval lanes.
- [ ] Batch Qdrant dense lane requests per corpus where client/server contracts
  support it.
- [ ] Keep exact alias payload lookup logically separate from dense requests.
- [x] Cache vocabulary resolution by query hash, selected corpus set, planner
  version, and artifact epoch. **[`services/retriever/
  vocabulary_cache.py` + resolve() wiring: key = normalized query + ordered
  lane queries + sorted corpus set + tier/top-k/disabled/exclusions +
  per-corpus epoch; keyed on resolver VERSION via cached payload's version
  field; TTL default 300s, LRU 512, env-tunable. LIVE-VERIFIED 2026-07-13
  in the deployed backend container against live artifacts: identical
  resolve() pair = cold miss 2.494s -> hit 0.002s, cache.hit=true, stats
  hits 0->1, probe exit 0]**
- [x] Invalidate cache on lexicon/tree/document artifact changes.
  **[epoch bumps from lexicon materialization (full + affected) and
  corpus-lexicon deletion; cross-process worker writes are bounded by the
  TTL, stated explicitly in the module contract. LIVE-VERIFIED 2026-07-13:
  bump_corpus_epoch rotated the cache key (070c7428… -> 3c35e72b…),
  invalidations=1, and the next identical resolve() recomputed (miss,
  0.21s) instead of serving the stale entry]**
- [ ] Capture a lexical/exact-only resolver baseline before adding deterministic
  multi-point representations so each representation method must prove lift.
- [ ] Treat this section and the P1.1 baseline as release prerequisites for the
  P2.2 multi-point projection; do not hide an 18-second resolver behind more
  vectors.

Acceptance:

- [ ] Vocabulary p50/p95 improve without concept recall regression.
- [ ] Repeated conversational queries hit the cache safely.

### P1.8 Finish Sidecar Isolation

- [x] Reranker performs real inference warmup at startup.
- [x] Embedder provides priority/FIFO admission classes.
- [x] Query calls use the interactive embedding workload class.
- [x] Add a bounded real-inference embedder startup warmup using the deployed
  query contract; model loading alone is not sufficient.
  *(merged @7f8e7b2; runtime sidecar copy redeployed + live-probed
  2026-07-13: fixed neutral batch ran through the serving-path admission
  gate at backfill class, warmup complete in 0.045s, dim=1024; in-container
  warmup+priority-gate suites 10 passed, exit 0)*
- [x] Separate embedder liveness, model-loaded health, and inference-ready
  status so deployment gates cannot confuse them.
  *(merged @7f8e7b2; live :8082/health 2026-07-13 carries distinct
  liveness=true / model_loaded=true / inference_ready=true keys; backend
  health checker prefers inference_ready when present)*
- [x] Expose `warmup_complete`, duration, vector dimension, and model/version in
  readiness diagnostics without exposing request content.
  *(merged @7f8e7b2; live :8082/health 2026-07-13: warmup{complete:true,
  duration_s:0.045, vector_dim:1024, model:Qwen3-Embedding-0.6B-mxfp8,
  error:null} — warmup batch is a fixed constant, never request content)*

### P1.9 Qdrant Hot-Path And Contention Audit

Measured baseline (2026-07-12, Qdrant 1.18.0, largest active corpus):

- Warm dense child search at `k=85`: about 6 ms p50 without payload and about
  14 ms p50 with the full payload; 30-run p95 stayed below 20 ms.
- Full payload transfer was about 292 KB for 85 hits; a bounded payload selector
  reduced it to about 148 KB.
- Qdrant used 6.63/8 GiB (82.9%) with no optimizer job active.
- The node held 59 collections / about 2.67M vector representations, including
  37 empty collections and non-active nonempty collections.
- Hot search clients already use internal gRPC. Summary-tree searches already
  use `query_batch_points`; broad vocabulary/funnel batching remains incomplete.

- [ ] Add per-stage Qdrant wall/server timing, result bytes, collection, filter
  shape, and candidate count to retrieval traces.
- [ ] Replace `with_payload=True` on hot candidate searches with explicit field
  selectors, preserving every field consumed by ranking, hydration, citations,
  and diagnostics.
- [ ] Batch compatible dense vocabulary and tree requests by collection/corpus;
  do not merge exact alias lookup into the approximate vector request.
- [ ] Audit all active query filters against payload indexes before enabling
  strict mode. `summary_model` is currently filtered but not indexed.
- [ ] Audit the 29 payload indexes repeated on each route collection; retain hot
  filter fields in RAM and remove only indexes proven unused by retrieval,
  graph, readiness, deletion, and repair paths.
- [ ] Define interactive and bulk-ingest Qdrant profiles. Under concurrent
  writes, benchmark smaller upsert batches and `max_optimization_threads=1`
  before changing defaults.
- [ ] Do not enable `prevent_unoptimized` until every write explicitly uses the
  intended `wait` semantics and readiness proves deferred points are searchable.
- [ ] Keep current single-shard/single-replica topology on this one-node Mac;
  replicas and extra shards are not a free local speedup.
- [ ] Pin the Qdrant image version after the performance contract is validated;
  do not deploy `latest` as the production data-store contract.

Acceptance:

- [ ] Qdrant remains below its memory warning gate during normal retrieval.
- [ ] Candidate payload bytes fall without losing ranking or citation fields.
- [ ] Query latency during a bounded ingest stays within the measured target and
  ingestion remains durable/queryable.
- [ ] End-to-end latency reporting distinguishes Qdrant time from embedding,
  planning, vocabulary, reranking, Mongo hydration, Neo4j, and synthesis.
- [ ] Verify ingestion/backfill calls consistently use lower-priority classes.
- [ ] Add or verify reranker-side priority admission.
- [ ] Measure whether keepalive provides value after startup warmup.
- [x] `MLX-EVAL-STABILITY-V1`: make sustained frozen-eval embedding fail
  closed before scoring rather than timing out mid-run. The backend query
  deadline must permit the 30-second local-client contract, local timeouts
  receive one bounded retry, and an eval-batch preflight must health-check and
  warm the production pooled connection. Acceptance is focused/adjacent tests
  plus a true-exit 100-call sustained soak with zero failures.
  *(GREEN on review branch `codex/mlx-embedder-stability-20260717`: isolated
  focused/adjacent tests 24 passed; canonical container tests 15 passed;
  100/100 sustained calls succeeded at concurrency 3 with zero failures,
  2.839-second wall time, and true `EXIT=0`. Frozen eval specs/scorers remained
  read-only. Receipt:
  `docs/MLX_EMBEDDER_STABILITY_SOAK_RECEIPT_2026-07-17.md`.)*
- [ ] Evaluate separate ingestion embedder capacity only if contention persists.
  *(merged + deployed both accounts (k695blmk52oscm/hlp9h3o4zd0v4d) + live-verified 2026-07-13: dim 1024, dual-account routing works; PROMOTION GATE: local-mxfp8 vs remote-fp cosine = 0.98, so runpod embed mode is approved for whole-corpus/bulk experiments only until a held-out recall A/B clears mixed-source use — never mix embedding sources within one collection)*

Acceptance:

- [ ] The first interactive embedding after service startup does not pay model
  compile/inference warmup cost.
- [ ] Backfill cannot force interactive embedding/reranking beyond agreed p95.
- [ ] Cold-start and concurrent-query tests no longer degrade unexpectedly.

### P1.9 Make Reranking Adaptive

- [ ] Measure reranker latency by candidate count and token length.
- [ ] Generate query-relevant excerpts before cross-encoder scoring.
- [ ] Select candidate budget by answer shape, score cliff, and retrieval
  uncertainty.
- [ ] Preserve broader pools for cross-domain and list questions.
- [ ] Evaluate cascade or late-interaction alternatives before model changes.

Acceptance:

- [ ] Rerank latency improves materially with no held-out recall/nDCG loss.
- [ ] Fast is meaningfully faster than Hybrid while remaining useful.

## P2 - Universal Concept And Vocabulary Layer

### P2.1 Version The Universal Concept Contract

- [ ] Keep one corpus-lexicon system; do not create a parallel concept store.
- [ ] Define canonical name, aliases, abbreviations, definitions, contextual
  usages, applications, components, source IDs, entity IDs/types, and evidence.
- [ ] Add field-level provenance and validation status.
- [ ] Separate source-extracted facts from generated user-language material.
- [ ] Add salience counters: evidence count, document count, graph support, and
  resolver hit count.
- [ ] Add chunk-derived `associated_concepts` using parent/chunk co-occurrence,
  PMI or log-likelihood weight, support count, and the intersecting evidence
  parent/chunk IDs. Keep these associations distinct from typed factual graph
  relations.
- [ ] Compute co-occurrence through a bounded parent/chunk-to-concept inverted
  index with minimum support, significance, specificity, and per-concept
  neighbor caps; never perform an unbounded all-concept pairwise scan or let
  one boilerplate parent create a combinatorial association hub.
- [ ] Add source-backed `usage_frames` by harvesting KWIC sentences around
  canonical/alias mentions and extracting dependency frames such as
  `use X to`, `X prevents`, and `apply X when`; retain sentence boundaries,
  source IDs, parser/pattern version, and confidence.
- [ ] Add a `semantic_profile` that aggregates supporting-parent
  `semantic_chunk_type` distributions and validated mechanism IDs so the card
  records whether the corpus treats the concept as a definition, procedure,
  principle, warning, example, or another evidence shape.
- [ ] Store `df`, `corpus_n`, specificity/IDF, support distribution, and hub
  status per entry. Generic/high-degree concepts may retain evidence but cannot
  gain expansion or representation rights without passing specificity gates.
- [ ] Version four provenance classes independently: `extracted` (provider or
  deterministic source extraction), `derived` (joins, frames, templates),
  `attested` (validated real user phrasing), and `generated` (optional LLM).
- [ ] Keep Mongo `corpus_lexicon` authoritative and make every Qdrant concept
  point an idempotent, fully rebuildable projection with projection/artifact
  epoch and deterministic point identity.
- [ ] Require `retrieval_eligible=true`, valid source ownership, evidence
  bindings, and specificity admission before any entry emits query-facing
  representation points.
- [ ] Use globally stable concept identity while retaining `corpus_id` and
  source ownership.
- [ ] Add versioned source-local `sense_id` records before mapping ambiguous
  surface forms to a stable global concept identity; preserve evidence and
  mapping type (`exact|close|broad|narrow|related`) and never treat a lexical
  mapping as a causal assertion.
- [ ] Keep source/genre, domain, mechanism, epistemic status, and context as
  separate typed facets. Do not collapse them into sibling universal themes or
  let a broad domain/theme label prove cross-domain transfer.
- [ ] Add one versioned controlled domain registry and deterministic resolver.
  Treat the research draft's 16 macro-domain families as a seed application
  taxonomy, not a universal standard; LLM-proposed aliases/specializations stay
  provisional until existing-match, evidence-support, and human/policy gates
  resolve them. Never create a permanent domain node from one model response.
- [ ] Support evidence-bearing document/chapter/parent/claim domain profiles
  through bottom-up aggregation and top-down disambiguation, with local claim
  evidence dominant. Cardinality caps, aggregation weights, affinity priors,
  and resolver thresholds are versioned experiment parameters, never hardcoded
  universal facts or retrieval gates.
- [ ] Define `librarian_card.v0` fields and deterministic seed contracts:
  - `central_subjects`: lexicon/Ghost B entities and profile concepts.
  - `mechanisms_taught`: promotion artifacts and validated summary mechanisms.
  - `capabilities_developed`: source-backed application contexts/useful-for
    fragments with derivation provenance.
  - `problems_addressed`: only explicit definitional/problem-shaped evidence.
  - `transferable_principles`: specific high-salience concepts with multi-doc
    support.
  - `risks_or_likely_misuse`: empty in v0 unless explicit warning evidence
    exists.
  - `counterbalancing_concepts`: empty in v0 unless typed/evidence-backed.
  - `evidence_spans`: parent/chunk/tree IDs and available exact boundaries.
- [ ] Record `field -> method -> source IDs -> confidence` for every populated
  card field.
- [ ] Penalize generic/high-degree principle hubs using specificity/IDF and
  support-distribution checks before they can justify transfer.
- [ ] Publish slim Tier-0 card payloads only after pointer-integrity validation.

### P2.2 Build Two-Sided Multi-Point Concept Representations

The concept record is a two-sided join: query-shaped representations nominate
the concept, while source document/parent/chunk bindings identify where its
evidence lives. Vocabulary nominates; hydrated chunks still testify.

Prerequisites:

- [ ] Complete the P1.1 held-out baseline and contamination firewall.
- [ ] Complete P1.7 batching, vector reuse, resolver cache, epoch invalidation,
  and exact/lexical-only latency baseline.
- [ ] Select only P2.1 entries that pass retrieval eligibility, ownership,
  evidence, salience, and specificity admission.
- [ ] Pilot on one small strict-ready corpus with a configured maximum number of
  representation points per concept before projecting large corpora.

Deterministic representation projection:

- [ ] Emit separate points for `name_alias`, `definition`, `utility`, and
  `templated_question_problem` representation methods instead of forcing every
  query shape into one compromise gloss vector.
- [ ] Build deterministic questions/problems only from indexed fields and
  usage frames, such as `What is X?`, `How do you <usage frame>?`, and
  `How to <application context>?`; every point must remain span/source backed.
- [ ] Store `canonical_key`, `lexicon_id`, `corpus_id`,
  `representation_method`, provenance class, evidence IDs, artifact epoch,
  validation status, and `exploratory` on every point.
- [ ] Use deterministic point IDs so methods can be rebuilt, superseded, or
  removed independently without rebuilding the authoritative lexicon.
- [ ] Overfetch across representation methods, group by `canonical_key`, and
  score a concept by the strongest admissible representation (`max` with
  calibrated method policy), never by summing points or rewarding crowding.
- [ ] Prevent templated/generated/exploratory points from satisfying a required
  answer lane without original-query support or source-evidence descent.

Attested-query feedback loop:

- [ ] Persist per-message query hash, selected corpus scope, resolved concept
  IDs, seated documents, answerability, evidence coverage, artifact epoch, and
  retrieval diagnostics in durable storage rather than stream-only telemetry.
- [ ] Harvest a user's phrasing as `attested_query` only when the answer passes
  answerability/evidence gates and the concept can be traced to selected final
  evidence; increment `resolver_hit_count` deterministically.
- [ ] Store attested-query hash, timestamp, validation rule version, concept
  IDs, and evidence IDs without storing unnecessary sensitive conversation
  content.
- [ ] Define explicit opt-in, minimization/redaction, retention, deletion, and
  corpus-isolation policy for attested phrases and their embeddings; do not
  promote personal narrative text when a shorter concept-bearing phrase is
  sufficient.
- [ ] Treat a failed query followed by a successful user rephrase as a pending
  alias/representation candidate; never auto-publish it without validation.
- [ ] Reject every held-out/evaluator query hash from harvesting and verify the
  firewall before each evaluation run.

Optional generated amplification (Phase 4 only):

- [ ] Generate plain glosses, problem phrasings, goal phrasings, and example
  questions only from source-backed concept material after deterministic lift
  is measured.
- [ ] Validate structured output and record provider, model, prompt, schema, and
  evidence versions.
- [ ] Never create extractive fallback placeholders after provider failure.
- [ ] Mark generated matches exploratory unless original user language or an
  admissible attested representation independently establishes the concept.

Downstream contract:

- [ ] A concept hit may contribute source-backed expansion terms, document
  nominations through `source_document_support`, and normalized concept IDs to
  librarian-card overlap; it cannot replace child/parent evidence.
- [ ] Preserve corpus scope on every resolve point and globally merge
  cross-corpus results with corpus-local diagnostics and fair representation.

Acceptance:

- [ ] Report Recall@K, concept/document recall, nDCG/MRR, answer evidence
  coverage, storage, RAM, and p50/p95 for each representation method alone and
  cumulatively.
- [ ] Lay-language Recall@K improves on held-out questions without direct expert
  query regression.
- [ ] Representation crowding cannot outrank a stronger single concept because
  one concept owns more points.
- [ ] Hub/junk concepts emit no query-facing points and gain no expansion rights.
- [ ] Generated or templated phrasing drift cannot misroute required evidence.
- [ ] Attested harvesting contains zero held-out query hashes.
- [ ] Storage/latency increase remains inside the measured budget and the cache
  invalidates correctly after lexicon/tree/document epoch changes.

### P2.2c Query-Side Retrieval Optimization (owner gap-call 2026-07-14)

Design of record: `docs/RETRIEVAL_OPTIMIZATION_PLAN_2026-07-14.md` (owner store
layout: 16 Mongo collections, 8 vector families incl. illustrative claim
vectors, 13 Neo4j node types). Activation is family-by-family, each behind its
own before/after A/B.

- [ ] Per-family query-instruction registry (Qwen3 asymmetric: raw documents,
  instructed queries) — owner approves embedding_instruction_registry v1;
  instruction_version recorded in every ProjectionManifest embedding_profile;
  instruction changes = new version + recall A/B, never silent.
- [ ] Mode->family search matrix as versioned policy data (FACTUAL/EXPLANATORY/
  CROSS_DOMAIN/EXPLORATORY/CREATIVE_TRANSFER/CONTRAST); recall permission
  separate from grounding permission (permission ladder enforced at assembly).
- [ ] Lineage dedupe before fusion: one parent reachable via child/summary/
  claim/latent routes = ONE candidate with a family-hit profile; unit-tested.
- [ ] Cross-family fusion is rank-based; raw cosine never compared across
  families; per-family calibration data recorded for later learned fusion.
- [ ] Rerank sees SOURCE TEXT only — recall lanes must not leak generated
  descriptions (latent/motif text) into cross-encoder scoring.
- [ ] Per-stage latency budgets + per-family Qdrant server-time measurement
  (separate from embed/fuse/rerank/hydrate), reported in retrieval diagnostics.
- [ ] Graph expansion budgets (<=2 hops, <=200 nodes, partition-permissioned
  per mode) as recipe params.
- [ ] `FOUR-LANE-TIER0-ROUTER-V1`: dark-ship one document-level routing stage
  ahead of evidence retrieval. Fuse independently attributable lexical
  title/summary/heading, semantic document-summary/digest, child-hit rollup,
  and T9.1-resolved associative ontology lanes with per-lane seat quotas,
  threshold spillover, associative seat protection, and divergent-profile
  surface-match demotion. Purchased `SemanticDigestV1` projections are routing
  signals only and never chunk competitors; parent summaries are never newly
  embedded for the child-rollup lane. Optional one-call subquery decomposition
  is separately gated and defaults OFF. Acceptance is the frozen three-tier
  OFF/ON suite with no direct/lay regression and relationship remaining green,
  plus the immutable six-query bridge diagnostic preregistered at
  `backend/evals/tier0_bridge_diagnostic_v1.json`, including per-lane
  attribution. All flags remain default OFF until a separate promotion.

Acceptance:

- [ ] Each activated family earns its seat: lay-language/cross-corpus doc-hit
  improvement with no shape regression on the frozen suite, else stays dark.
- [ ] Permission tests: a mode's forbidden family contributes zero candidates;
  FACTUAL answers ground only in validated source-backed claims/evidence.
- [ ] Whole-query latency within +20% of tier baseline at every activation.

### P2.2d Forced Latent Sweep — comparison experiment (owner-adopted 2026-07-14)

Owner design: systematic domain-conditioned latent generation — for each
artifact, EACH swept domain yields a generated latent concept binding the
artifact to that domain, or an explicit abstention. Supply-side feed for
CROSS_DOMAIN / EXPLORATORY / CREATIVE_TRANSFER and cross-corpus bridges.

- [ ] Sweep recipe (versioned): candidate domains per artifact = affinity
  priors + embedding neighborhood top-N + exactly ONE wildcard distant domain
  per pass (structured serendipity); budgets ride latent_concept_policy
  (generation/storage/usage separation).
- [ ] Output contract: domain-keyed LatentConceptProposal via the P2.5c
  gateway (supporting_claim_ids validated; abstention = empty array is a
  rewarded output; never marked validated by the producer).
- [ ] Provenance: derivation_method=`llm_forced_sweep` on every swept
  proposal — never mixable with spontaneous digest proposals or corroborated
  concepts.
- [ ] Corroboration path: swept candidates that bind superframe roles/motif
  stages over real claims are promotable per the permission ladder; unbound
  ones stay candidates.
- [ ] Storage: domain-keyed latent families in the registry/rollup (each
  domain's generated latent concept addressable per artifact).

Acceptance (the comparison test):

- [ ] A/B on the frozen suite + cross-corpus shapes: forced-sweep candidates
  ON vs deterministic-only bridge mining — CROSS_DOMAIN/EXPLORATORY answer
  improvement with no FACTUAL regression and negatives 5/5; per-domain sweep
  yield + abstention-rate census reported.
- [ ] Cost receipt: calls per artifact within recipe budget; empty-yield
  domains identified for prior tuning.

### P2.3 Add Versioned Qwen3 Retrieval Embedding Instructions

Owner directive adopted 2026-07-13: Qwen3 retrieval embeddings use distinct,
versioned query and index roles. The proposed task text was
`the following text is a document for rag retrieval` for indexed material and
`given the user question, retrieve the most relevant information` for queries.
Primary-source review of Qwen's shipped SentenceTransformers config resolves
the indexing side to an empty document prompt: indexed material remains raw,
while the query task is serialized in Qwen's `Instruct: ...\nQuery:...`
envelope. A non-empty document prompt is a separate custom profile requiring a
clone/re-embed/evaluate/cutover and is not mixed into the canonical profile.

- [ ] Introduce explicit query and document embedding roles distinct from
  concept/tree/schema embeddings; apply the query instruction exactly once and
  the canonical empty document prompt at the shared client boundary across
  local, Modal, SiliconFlow, and RunPod lanes.
- [ ] Make instructions model-specific and versioned, and persist the embedding
  profile with the corpus/index contract so incompatible vectors cannot mix.
- [ ] Keep raw input, embedding role, model ID, and instruction/profile version
  in every embedding cache key.
- [ ] Route retrieval queries with the profile frozen on the target collection;
  legacy collections remain on their legacy query contract until migrated.
- [ ] If a custom non-empty document prompt is later promoted, re-embed
  document, parent-summary, concept, tree, card, and schema lanes only through
  a resumable clone/verify/cutover migration; never write prompted and
  unprompted vectors into the same collection.
- [ ] A/B test the Qwen3 instruction profile against the held-out three-tier
  suite before the first production corpus cutover.

Acceptance:

- [ ] Query and document serialization is byte-exact in unit tests and identical
  across every embedding provider used by a corpus.
- [ ] Recall improves without corrupting document-vector compatibility; the
  active query profile always matches the indexed collection profile.

Experiment receipt (2026-07-14): `universal-v1` was **REJECTED WITH
EVIDENCE** on the Fast-first gate. It improved several rows and the completed
naïve cohort's mean recall, but lost q032's naïve document hit
(`true/.200 -> false/.000`) and raised same-ID partial mean latency by 17.9%.
The candidate was stopped at 32/58, reverted before reporting, and
`baseline_live_v0` remains live. The durable registry resolver,
instruction-version cache isolation, suffixed A/B harness, and container-only
revert path are deployed and proven. A new wording requires a NEW registry
version and the same gate; it is optional Track B and does not preempt the
core spine. Receipt:
`docs/baselines/T56_QWEN3_UNIVERSAL_AB_2026-07-14.md`.

## P2 - Extraction And RunPod Parity

### P2.4 Negation And Relation Correctness

T8.3 external limit (2026-07-14): the legacy `_validate_evidence` contract is
polarity-blind because it receives only `(evidence_phrase, chunk_text)` and is
shared by entity, fact, and relation gates. It has no predicate offsets,
claim polarity, sentence identity, dependency parse, or parser; the current
RunPod wire defaults to `blank:en`. T8.3 therefore preserves an evidence
phrase's own `not`/`no`/`never` tokens without pretending to infer attachment,
and performs dependency-attached polarity assessment only in the compiler
sidecar. Before any live P2.5/T-MAIN enforcement, the wire migration must
thread predicate offsets/polarity/parser provenance and pin the trained spaCy
model through the required blue-green canary. This prerequisite remains open.

- [x] Preserve negation tokens during evidence-overlap validation.
- [x] Add `negated` and evidence-sentence boundaries to the relation artifact.
- [x] Parse only emitted relation evidence sentences unless benchmarks justify
  full-document parsing.
- [ ] Ship and pin any required spaCy model in the RunPod contract.
- [ ] Decide promotion policy for negated relations; retain evidence for audit.

### P2.5 Typed Relation Signatures

- [ ] Build a reviewed predicate domain/range compatibility table from accepted
  real edges.
- [x] Initially annotate `signature_valid` and violation reason; do not drop or
  remap edges automatically.
- [ ] Measure violation rates by provider/model/corpus/predicate.
- [ ] Promote hard/soft enforcement only after false-positive review.

### P2.5a Unified Claim/Assertion And Mechanism-Frame Contract

Architecture decision: this is a downstream, provider-neutral semantic layer
beside existing ERE, not a replacement extraction engine. It must extend the
planned T-MAIN assertion artifact rather than create a second semantic claim
store that can disagree with temporal assertions.

Feasibility receipt (2026-07-13): the 9-text/12-claim hand-labeled fixture,
separate local models, authenticated provider runs, and RunPod threshold sweep
are recorded in `SEMANTIC_EXTRACTION_PRODUCTION_READINESS_2026-07-13.md`.
Deterministic spaCy candidate compilation matched 11/12 claims with 1.0 modal
and polarity accuracy and 1.0 condition/exception recall. Current composed
GLiNER→GLiREL relation F1 was 0.1333; joint RunPod Relex was 0 at threshold
0.75 and 0.0976 at 0.40. Therefore the architecture is adopted but relation
promotion remains blocked. This is a feasibility receipt, not completion of
the unchecked production items.

- [ ] Freeze one versioned authoritative claim/assertion contract carrying an
  atomic proposition, claim type, modality, polarity, conditions/exceptions,
  context/scope, inference status, source version, and provenance.
- [ ] Require chunk-local exact `start`/`end`, quote, and source hash for every
  accepted explicit claim; fail validation unless the quote round-trips to the
  durable source exactly. Do not fabricate source-global/page offsets that the
  current parse artifact does not possess.
- [ ] Bind claim arguments to P2.1 source-local senses and stable lexicon IDs;
  keep lexical/sense mapping distinct from factual and causal relations.
- [ ] Define a small, versioned application frame registry with typed allowed
  roles, cardinality, direction, and validation rules. Treat the attachment's
  16 frames and one-primary/two-secondary cap as a canary hypothesis; permit
  explicit abstention and revise from corpus error analysis.
- [ ] Validate frame-specific role objects with strict typed/discriminated
  contracts and deterministic policy. A schema-valid parse is not evidence of
  truth and cannot bypass span/provenance checks.
- [ ] Represent `ASSERTED`, `ENTAILED`, `CROSS_PASSAGE_SYNTHESIS`,
  `STRUCTURAL_ANALOGY`, and `HYPOTHETICAL` explicitly; never project a derived
  or analogy artifact as an author assertion.
- [ ] Persist raw provider output plus schema/ontology/compiler/prompt/model
  versions and an idempotent extraction identity so validation can be replayed
  without paying for extraction again.
- [ ] Put the capture hook and shared post-extraction validator on every active
  provider lane before the scheduled PoC mass re-extraction; ERE outputs remain
  durable recall inputs and candidate role fillers.
- [ ] Pilot a deterministic-first candidate lane using a pinned trained spaCy
  parser as the scoped claim/qualifier compiler, optional GLiNER span
  candidates, and versioned registry rules for claims/domains/frames. Persist
  current GLiREL and joint RunPod Relex relations as observations only until a
  replacement/remediation clears relation gates. Compare every candidate lane
  against the current provider-LLM lane on identical labels; “runs without
  task-specific training” is not a quality conclusion or permission to retire
  the comparison baseline.
- [ ] Test the research draft's bounded parent semantic-digest call only after
  accepted child claims are available. It must cite supporting claim IDs for
  every domain/frame/motif/condition, and remain a separate recipe from current
  Ghost A until before/after quality and cost results justify migration.
- [ ] At activation, every Mongo consumer of
  `semantic_digest_claim_compilations` must request timezone-aware BSON and
  strictly revalidate the typed row; default naive datetime decoding is barred.
- [ ] Land annotate-only with its first named consumer and diagnostics; do not
  change graph or retrieval behavior until the held-out claim/frame gates pass.
- [ ] Keep Mongo authoritative, make Neo4j/Qdrant projections deterministic and
  rebuildable, and preserve the existing hierarchy and lexicon ownership.
- [ ] Create no new generic `RELATED_TO` edge from this layer. Preserve legacy
  edges as compatibility/candidate data until measured cutover and rollback
  evidence exists.

Acceptance:

- [ ] Accepted explicit-claim spans have 100% exact round-trip integrity.
- [ ] Report claim precision/span recall and frame macro-F1, core-role F1,
  abstention, and errors by frame/provider/corpus on hand labels.
- [ ] No synthesized, hypothetical, or analogy artifact is mislabeled
  `ASSERTED`/`EXPLICIT` in positive or negative controls.
- [ ] Provider parity, deterministic replay, and artifact identity are proven
  on the UGO canary before the PoC pair.
- [ ] The capture contract is live before any paid mass rebuild and does not
  change Fast/Hybrid/Graph results while annotate-only.

### P2.5b Canonical Semantic Artifact Envelope, Identity, And Projection Contract

Decision of record:
`docs/FINAL_SCHEMA_METADATA_ARCHITECTURE_2026-07-13.md`. This contract is the
front gate of S11 and applies to new semantic artifacts without forcing an
immediate rewrite of every legacy collection.

Executable-slice receipt (2026-07-13):
`models/semantic_artifacts.py` now proves canonical domain hashing, exact
`EvidenceRef`, provider-neutral `ObservationBundle`, reference closure, and a
candidate contract that rejects extractor self-promotion to `accepted`.
`services/ingestion/semantic_observations.py` proves the spaCy adapter and
candidate compiler. The full shared envelope, all hash namespaces, legacy
adapters, Mongo validators/indexes, outbox, manifests, and UGO integration are
still unchecked work below.

- [x] Implement one strict `polymath.artifact_envelope.v1` around linked typed
  artifacts; do not build one giant parent JSON or a provider-specific truth
  schema. *(T3.2: frozen strict envelope/nested contract, typed body,
  namespace hashes, immutable revision recipe, explicit knowledge status,
  and byte-exact cross-process goldens implemented.)*
- [x] Implement one canonical JSON serializer with explicit set-valued fields,
  recursive key ordering, UTC timestamp rules, finite JSON numbers, and no
  implicit `default=str` coercion. *(2026-07-14: models/hash_taxonomy.py
  canonicalize/canonical_json_v1; 21 tests green in-container EXIT=0 incl.
  byte-exact goldens, naive-datetime/NaN/str-coercion rejection, UTC
  conversion; senior-built while Codex ran Phase A.)*
- [x] Freeze distinct names and recipes for source-content, normalized-text,
  schema, registry, recipe, input-set, body, evidence-set, scope, motif,
  projection-profile, work, raw-output, logical-artifact, and revision hashes.
  *(2026-07-14: HASH_NAMESPACES in models/hash_taxonomy.py — 15 frozen
  namespaces with recipe contracts; distinctness + unknown-rejection tested;
  golden hashes frozen. Identifier-recipe golden vectors still pending —
  acceptance box remains open.)*
- [x] Separate stable logical `doc_id` from immutable `source_version_id`;
  preserve the current content-derived document ID as a compatibility alias and
  never guess version lineage from filename/title similarity.
  *(2026-07-14: models/identifier_recipes.py — all 8 spec recipes (logical-doc,
  source-version, hierarchy-node, claim, artifact-revision, work, raw-output,
  projection-point-UUID) with byte-exact goldens + the four lineage
  distinctions (duplicate bytes / changed version / same-title-distinct /
  no-inference-without-strong-key) + retry-reuse and stochastic-output
  separation; 13 tests green in deployed container, 43/43 across the P2.5b
  suite EXIT=0. Adapter/integration wiring = CP3 remainder.)*
- [ ] Bind new hierarchy-node identity to source version, hierarchy recipe,
  node type, and an honest coordinate/ordinal contract; never fabricate page
  or source-global offsets.
- [ ] Add a provider-neutral `ObservationBundle` for spaCy, zero-shot,
  provider-LLM, and legacy ERE candidate observations; observations cannot
  carry asserted knowledge status.
- [ ] Separate stable artifact ID, immutable artifact revision, deterministic
  work ID, execution attempt, and raw-output artifact so retry/model/output
  identity cannot overwrite semantic identity.
- [ ] Keep full accepted artifacts authoritative in Mongo and add durable
  projection-outbox intent/retry/reconciliation; no new request path may depend
  on untracked Mongo+Qdrant+Neo4j dual writes.
- [ ] Freeze a projection manifest/profile for every Qdrant/Neo4j family,
  including source schema hashes, representation role, embedding/profile,
  payload schema, quantization/search compatibility, and rollback predecessor.
- [ ] Keep current Ghost A/tree output explicitly typed as `RetrievalSummary`;
  create claim-grounded `SemanticDigest` as a separate recipe/revision until a
  measured cutover is approved.
- [ ] Ship adapters/dispositions for documents, source identity, parent/child
  hierarchy, Ghost B rows, parent summaries, summary-tree nodes,
  `corpus_lexicon`, librarian cards, Qdrant points, and Neo4j legacy edges.
- [ ] Store versioned domain/frame/motif registry snapshots and hashes; keep
  weights, caps, thresholds, and affinity matrices in recipe/policy versions,
  not semantic identity.
- [ ] Give every promoted metadata field a named filter, ranker, hydrator, or
  diagnostic consumer in the same phase; otherwise mark it capture-only dark
  data and block projection.
- [ ] Make accepted artifact bodies immutable; corrections create new
  revisions/assertions with explicit supersession, while validation/lifecycle
  state remains outside semantic body identity.

Owner rebuttal integration (2026-07-14, rulings C1–C4 APPROVED — see
docs/REBUTTAL_INTEGRATION_RESTATEMENT_2026-07-13.md and the FINAL_SCHEMA
"Owner rebuttal integration" addendum):

- [ ] Implement the permission-state ladder: candidates are RETAINED after
  Python normalization with differentiated permissions (ground / recall /
  expand / explain) and corroboration→promotion paths; provisional latent
  concepts and motif candidates are weighted soft-recall signals; only
  validated source-backed claims independently ground FACTUAL answers.
- [ ] Consume the owner's motif/superframe/domain registry schemas VERBATIM as
  versioned snapshot data (delivery pending — C4); no invented registry ids.
- [ ] Build the parent evidence-packet ASSEMBLER over existing structure
  (128-token children + structural parents stand; no re-chunking — C1).
- [x] GLiREL Stage-4 re-benchmark gate (C2): compiled-claim quality WITH vs
  WITHOUT controlled-label GLiREL candidates on the gold fixture. **REJECTED
  WITH EVIDENCE (2026-07-14):** `without_wins`; zero accepted controlled-label
  relations in both production-shaped and oracle-span arms, so relations stay
  observation-only and the deterministic compiler remains the typed-relation
  authority. See the T8.5 Implementation Log receipt.
- [ ] Context-enriched child embeddings ship as a P2.2 representation-point
  kind (C3), inheriting P2.2 prereqs/caps — not a new mechanism.
- [ ] Enumerate ProjectionManifests for all owner-ruled families: Qdrant
  (source-child, context-enriched child, parent-summary, latent-concept,
  motif/analogy), Mongo hybrid signals (source text, explicit aliases,
  generated aliases, domain labels, superframe labels, latent concepts,
  assignment states), Neo4j (asserted claims, validated semantic, provisional
  expansion, analogy).
- [ ] Implement query modes (FACTUAL / EXPLANATORY / CROSS_DOMAIN /
  EXPLORATORY / CREATIVE_TRANSFER / CONTRAST) as permission mixes over
  assignment states and index families — policy data, not hardcoded branches.

Acceptance:

- [ ] Canonical JSON, every hash namespace, and every identifier recipe have
  byte-exact golden test vectors and cross-process replay parity.
- [ ] Legacy fixture adapters produce contract-valid equivalents without
  rewriting or relabeling legacy observations as accepted claims.
- [ ] Logical-document and source-version tests distinguish duplicate bytes,
  changed versions, unrelated same-title files, and explicit owner lineage.
- [ ] Mongo plus one projection manifest can reproduce exact Qdrant/Neo4j
  identity sets; interruption/retry creates no duplicate semantic artifact.
- [ ] Annotate-only canary leaves existing Fast/Hybrid/Graph results and legacy
  hydration behavior unchanged.
- [ ] P2.5b passes on UGO before the PoC pair's paid mass re-extraction or any
  production graph/vector cutover.

### P2.5c Structured-Output Gateway (semantic digest calls)

Design of record: `docs/STRUCTURED_OUTPUT_GATEWAY_SPEC_2026-07-14.md`
(owner-delivered 2026-07-14). RunPod = extraction; API via THIS gateway =
digests. Registry-independent; first S11 build unit alongside P2.5b envelope.

- [x] `SemanticDigestV1` pydantic contract (portable subset: shallow, closed,
  fully-required, enum-driven, versioned) + `model_json_schema()` as the single
  source of truth; golden schema-hash test. *(T4.1: fully-required §2-wins
  erratum applied; exact golden + fresh-process replay; 90 passed / 3 skipped
  across the adjacent semantic-contract suite.)*
- [ ] Capability ladder: Tier1 native strict json_schema via LiteLLM
  `supports_response_schema()`; Tier2 grammar-constrained local fallback
  (llama.cpp Metal path; MLX not trusted until proven); Tier3 forced tool-call;
  Tier4 JSON-mode+validate+one-retry (last resort, flagged in provenance).
  *(T4.3 implements Tier1 and fail-closed Tier4. T4.4 adds a versioned Tier3
  forced-tool path and a runtime-probed route registry. Flash rejected Tier1,
  Tier4 was structurally unreliable, and Tier3 accepted only a partial set
  before exhausting semantic repair; LongCat passed only a tiny Tier3 tool
  probe. `verified_digest_path` therefore remains null. Tier2 remains a public
  clear-failure stub, so this combined box stays open.)*
- [x] Python semantic validator (claim refs exist+belong to parent; registry
  ids exist or explicitly candidate; frames MF01-16 with support; latent needs
  claims in claim-grounded mode; motifs >=2 frames over proposed/validated
  frames; no self-links; LLM proposals never source-observed/validated).
  *(T4.2: pure typed packet context + deterministic location-indexed errors;
  39 focused and 128 adjacent tests passed.)*
- [x] Targeted repair loop: attempt-2 sends exact validation errors under the
  SAME constrained schema; failures -> dead-letter queue, never canonical
  writes; deterministic-safe fixes in Python only. *(T4.3: structural and
  location-indexed semantic failures exercise exactly one repair; second
  failure and transport failure write only the noncanonical DLQ. T4.4 pins
  independent `parent-digest.v5` and `parent-digest-repair.v2` identities;
  Tier3 repair reuses the same forced tool and requires all 12 fields at the
  argument root.)*
- [x] Determinism/provenance record on every generation (model/runtime/
  tokenizer/template/schema/prompt hashes, temp 0, input/output hashes) +
  cache keyed on input+model+schema+prompt+runtime. *(T4.3: all five cache-key
  inputs have identity-flip tests; accepted cache entries are structurally and
  semantically revalidated before reuse. T4.4 adds the repair-prompt hash to
  provenance and the combined prompt identity without changing the five cache
  key dimensions.)*
- [x] Prompt/schema separation: compact rule prompt; schema only via the
  constrained API surface (`response_format` or a Tier3 forced-tool
  definition); scope discipline (digest never emits entities/triples/claims/
  Cypher/records/payloads/embeddings). *(T4.3/T4.4: the generated Pydantic
  schema never appears in initial or repair prompt text.)*

Acceptance:

- [ ] UGO parent-packet canary: 10 packets through Tier1 with 0 structural
  failures, semantic-validator receipts, >=1 exercised targeted repair, >=1
  dead-letter demonstration (synthetic), provenance rows complete. *(T4.4
  closed at an external provider limit: 0/5 configured routes accepted native
  `json_schema`; flash Tier4 was structurally unreliable and Tier3 stopped
  after 3/10 accepted noncanonical rows plus one two-attempt semantic DLQ.
  The synthetic DLQ step was not reached. See the T4.4 baselines.)*
- [ ] Ladder downgrade test: same packet through Tier1 and Tier4 produces
  schema-identical shapes; Tier4 flagged in provenance. *(Not reachable
  without a provider-verified Tier1 digest path; retest at CP9 preflight.)*
- [x] Cutover stays gated: Ghost A `RetrievalSummary` path untouched until the
  measured cutover ruled in P2.5b. *(T4.4: Ghost A changes=0; four exact
  Mongo/Qdrant/Neo4j snapshots stayed number-for-number unchanged.)*

### P2.6 Engine Parity And Provenance

- [x] Version a shared extraction artifact contract across cloud, local, and
  RunPod engines. *(T9.4 deterministic slice: strict
  `candidate_extraction_artifact.v1` adapters cover cloud, private local,
  legacy local, and RunPod; authority is explicitly executor-proposed and
  owner-ratifiable.)*
- [x] Record extraction engine, model ID, contract hash, field-level methods,
  offsets, confidence, and evidence. *(T9.4: native/runtime/wire identities and
  exact-or-unavailable offset provenance are mandatory in the shared shape.)*
- [x] Decide whether deterministic facts are supported per engine; do not make
  noisy facts mandatory for queryability. *(T9.4 v1 records current truth:
  legacy-local deterministic facts supported; cloud/private-local and RunPod
  not deterministic; facts required for queryability is false for every
  engine.)*
- [x] Leave `object_kind` blank when it cannot be grounded; do not fabricate it
  from coarse entity type. *(T9.4 adapter preserves it only with an exact
  caller-supplied source quote containing the proposed kind.)*
- [x] Add relation-cue derivation only when source-evidenced. *(T9.4 adapter
  carries a cue only when it maps to one unique exact source span.)*
- [x] Keep deterministic alias/definition rules in shared backend validation.
  *(T9.4 adapters ignore provider aliases/definitions and recompute through
  `services.ingestion.enrich`.)*
- [x] Run the same post-extraction lexicon projector (co-occurrence, usage
  frames, semantic profile, DF/specificity, and representation admission) for
  cloud, local, RTX, and RunPod artifacts so provider choice cannot change the
  query-translation contract. *(T9.4 current-field implementation: strict
  candidate artifacts from cloud, local, legacy-local/RTX, and RunPod enter
  one compatibility adapter and the existing engine-blind document/corpus
  projector. Same-input synthetic parity is field-identical after removing
  only `updated_at`; ownership, duplicate/missing chunks, contract/source
  drift, and failed artifacts reject. This closes the shared-projector
  implementation for fields that exist now, not a claim that the planned
  P2.1/P2.2 usage-frame/semantic-profile/DF fields already exist. Those future
  fields must land only in this projector; actual engine-output/corpus-scale
  parity remains open under P2.7.)*

### P2.7 RunPod Production Validation

- [x] RunPod Flash contract, settings, backend adapter, and worker code exist.
- [ ] Deploy a pinned endpoint artifact.
- [ ] Pass canary, 100, 500, and 5,000-chunk yield/quality/budget gates.
- [ ] Compare entity, relation, evidence, ontology, graph-promotion, and failure
  rates against current cloud/local paths.
- [ ] Verify retries never erase valid summaries, vectors, or graph artifacts.
- [ ] Wire extraction/concept readiness into corpus strict-readiness.

Quality-gate receipt (2026-07-13): the joint
`knowledgator/gliner-relex-large-v0.5` endpoint completed all 9 adversarial
samples without transport/schema failure, but relation F1 was 0 at the current
0.75 threshold. Lowering the threshold to 0.40 emitted 32 relations with 2
true positives and 30 false positives (precision 0.0625, F1 0.0976). The
100/500/5,000 paid escalation was correctly stopped at the semantic gate; it
must not resume until model/label/task remediation clears the expanded unique
quality fixture. The 540 repeated-input local test is capacity-only and cannot
satisfy this section's corpus-scale acceptance.

Acceptance:

- [ ] RunPod is called production-ready only after corpus-scale measured parity.

### Active PoC Scope (owner directive 2026-07-13 — grounding for all testing)

- **Designated proof-of-concept corpora: `markbuildsbrands_transcripts` and
  `ecommerce_AI_FILM_SCHOOL`** — all cross-corpus retrieval testing,
  ingestion experiments, extraction bursts, and librarian/shelf iteration
  target these two until the owner widens scope. They are the cross-corpus
  pair for eval interpretation (film craft x marketing = real bridge
  material).
- **`polymath_v2` is LEFT ALONE for heavy operations** (its P0.1 repair is
  complete and verified; sheer size makes it wrong for PoC iteration):
  no re-extraction, no reingest, no mass backfills without a new owner
  directive. Read-only participation in the held-out eval suite continues.
- **`UGO_CORPUS` stays the 1-document canary** (first target for any new
  pipeline before the PoC pair).
- Disposition matrix consequence: v2 = `projection-only (frozen)`;
  mark + ecommerce = active `re-extract-only` PoC targets (pending T-HOOK-1);
  the 5,000-chunk burst gate runs against the PoC pair, not v2.

- [ ] Queue-based extraction workers report image/pipeline version in the
  wire response (adopted 2026-07-14 from CP1-D4 deploy verification gap —
  local sidecars expose /health pipeline_version, RunPod /runsync has no
  version surface; bundle with the next worker change).

### P2.7b RunPod Burst Orchestration (owner design, adopted 2026-07-13)

- [ ] Per-corpus disposition matrix BEFORE any mass job: each active corpus is
  classified `reingest` (re-parse/re-chunk — bad heading_path/OCR; rebuilds
  chunk IDs so summaries/vectors/extractions follow) vs `re-extract-only`
  (chunking sound; summaries/vectors preserved) vs `projection-only`. No
  reindex or extraction spend on a corpus marked `reingest`.
  **[IN CODE — evidence gathered 2026-07-13, awaiting owner sign-off:
  docs/DISPOSITION_MATRIX_2026-07-13.md — recommended: ecom = reingest subset
  (Group A + optional B) + projection repair, mark = re-extract-only, UGO =
  projection-only, v2 = projection-only (frozen); page_start needs a
  prov-capture code change, reingest alone will NOT add it]**
- [ ] Chunk-complete barrier: for a corpus ingest (e.g. 300 files), ALL valid
  children are parsed, metadata-extracted, and fully chunked BEFORE the first
  RunPod dispatch; extraction never interleaves with chunking.
- [ ] Saturating burst dispatch: build the full eligible-chunk manifest at the
  barrier, size request_batch_size x request_concurrency so queue depth keeps
  max_workers saturated end-to-end (pods bill by time — throughput IS cost),
  autoscale workers to awaiting-job volume, drain, then idle_timeout scales
  to zero. Record chunks/sec, worker-seconds billed, and cost per 1k chunks
  per burst.
- [ ] Burst runs the full local stack remotely: GLiNER-ReLEX + spaCy windows +
  deterministic python normalization in the worker; results return through
  the existing ontology/evidence/promotion gates only.
- [ ] Sequencing: T-HOOK-1 (temporal capture in the wire contract) lands
  before the first corpus-scale burst; 5,000-chunk gate validates the burst
  profile itself (not just extraction quality).

### P2.7c Multi-Account RunPod Routing (owner directive 2026-07-13)

- [x] Settings support N RunPod accounts (each API key = a distinct account
  with its own endpoint, quota, and billing): additive
  `ingestion.runpod_flash.accounts[]` {name, endpoint_id, enabled,
  max_workers, request_concurrency, weight} with per-account encrypted keys
  in the shared key store; legacy single endpoint_id + `api_keys.runpod`
  keeps working as the "default" account.
  *(merged + live-verified 2026-07-13: 2 accounts, 100-chunk run split 2/2 batches, 0 failures/failovers)*
- [x] Dispatch routes request batches across enabled accounts
  (least-in-flight, weight-tiebroken), per-account concurrency semaphores,
  bounded failover of a failed batch to another account, per-account batch
  counts in diagnostics — combined burst throughput = sum of accounts.
  *(merged + live-verified 2026-07-13: 2 accounts, 100-chunk run split 2/2 batches, 0 failures/failovers)*
- [x] Registration helper reads the key from env only (never argv/logs) and
  encrypts at rest; benchmark exercises routing across all enabled accounts.
  *(merged + live-verified 2026-07-13: 2 accounts, 100-chunk run split 2/2 batches, 0 failures/failovers)*

### P2.8 Direct Concept-To-Document Grounding

- [x] Concept provenance and expansion-lane document hints exist.
- [ ] Extend provenance-backed routing to direct core-lane concept matches.
- [ ] Connect trustworthy concept provenance to final document-anchor
  protection.
- [ ] Require profile presence and validated source support.
- [ ] Detect polluted concept cards that would mis-reserve documents.

## P3 - Semantic-Relational RAPTOR And Cross-Corpus Bridges

### P3.1 Pilot Claim And Mechanism-Frame Routing

- [ ] Expand P1.1 with a separately held-out, manually labeled cross-domain
  slice containing positive, near-miss, reversed-causality,
  same-word/different-sense, harmful-analogy, and no-answer cases across at
  least three unrelated domains; the current two cross-domain questions are
  insufficient.
- [ ] Start on UGO as the pipeline canary, then the designated mark/ecommerce
  PoC pair; keep `polymath_v2` frozen for heavy operations.
- [ ] Consume only validated P2.5a claim/assertion and frame artifacts with
  exact evidence, scoped senses, ontology/policy version, and corpus ownership.
- [ ] Compare the proposed 16-frame registry against observed corpus coverage,
  overlap, abstention, and error; do not call the inventory universal until it
  demonstrates recurrence across at least three unrelated domains.
- [ ] Keep source/genre, domain, mechanism, epistemic status, and context as
  separate features; no domain or theme label can prove a transfer.
- [ ] Retain soft, nonexclusive themes as an experimental routing baseline only;
  never make a single theme or frame a hard filter over downstream retrieval.
- [ ] Use frame/motif hits to nominate documents and evidence paths; only
  hydrated source evidence may support synthesis.
- [ ] Preserve raw child, summary/tree, lexical, original-query, concept, and
  current librarian-card lanes throughout the pilot.
- [ ] Diagnose selected claims, frames, roles, abstentions, candidate motifs,
  rejected mappings, evidence chains, and contributions to final evidence.
- [ ] Evaluate document/evidence Recall@K, nDCG/MRR, focused-query regression,
  broad/cross-domain coverage, hierarchical domain F1/proposal rate,
  storage/write cost, and p50/p95.

Acceptance:

- [ ] Frame and role quality clears preregistered labeled-set gates with
  provider/corpus breakdown and an honest abstention rate.
- [ ] Cross-domain evidence recall improves over current mechanism overlap and
  soft-theme baselines without direct/focused-query regression.
- [ ] Fast, Hybrid, and Graph corpus isolation and negative controls remain
  green; original-query evidence cannot be hard-filtered away.
- [ ] Only after success may mechanism-frame routing become a production lane.

### P3.2 Deterministic Motif And Analogy Bridge Cards

- [ ] Canonicalize ordered frame IDs, typed core roles, edge direction,
  polarity/state transition, scope, and scale into a readable, versioned motif
  record plus deterministic fingerprint; a hash is identity, not proof.
- [ ] Use exact fingerprint buckets for high-precision nomination and a bounded
  typed structural comparison for partial candidates; cap candidate work and
  never run an unbounded all-claim pairwise scan.
- [ ] Accept a bridge only with two-sided exact evidence, compatible core roles
  and causal direction, source-local sense compatibility, explicit invariant,
  explicit break/non-transfer conditions, and no forbidden transformation.
- [ ] Calibrate any scoring weights and thresholds on the labeled cross-domain
  slice. The attachment's `0.30/0.25/...`, `0.75`, and example `0.83` are
  hypotheses, not defaults or receipts.
- [ ] Preserve explicit `ASSERTED`, `SYNTHESIZED`, and `ANALOGY` status and
  parent derivations; analogy is non-transitive.
- [ ] Compute analogies query-time or retain only a bounded strongest set per
  claim. Do not materialize all pairwise links.
- [ ] Store endpoint claim, document, corpus, sense, frame/role, motif,
  temporal scope, evidence, invariant, break conditions, model/compiler,
  ontology, and policy versions on every accepted analogy card.
- [ ] Reuse the current deterministic librarian bridge-seat gate and evidence
  chains; an analogy card may route a shelf/document but never become an
  unsupported final claim.
- [ ] Exclude generic/high-degree `RELATES_TO` hubs and create no new generic
  relation from embedding, theme, word overlap, topology, or fingerprint alone.
- [ ] Evaluate non-obvious cross-domain recall, evidence coverage,
  false-bridge rate, harmful-analogy rate, latency, storage, and replay.

Acceptance:

- [ ] Every accepted analogy exposes both evidence chains, its role map,
  invariant, and non-transferable differences to diagnostics and synthesis.
- [ ] Reversed-causality, homonym, near-miss, and harmful-analogy controls fail
  closed.
- [ ] Analogy/bridge false-positive and harmful-analogy rates clear
  preregistered gates without reducing necessary corpus coverage.
- [ ] Embeddings, themes, exact words, topology, or motif hash alone can never
  materialize or seat a bridge.
- [ ] Legacy bridge logic remains available for measured A/B and rollback until
  the new artifact proves parity and improvement.

## P3 - Storage Architecture Experiments

### P3.3 Collection Consolidation - Migration

- [ ] Document why hrag, naive, graph, schemas, sparse, and payload contracts
  differ today.
- [ ] Treat the existing `polymath_children` migration as unfinished: the shared
  collection currently has only a few points and `QDRANT_SHARED_COLLECTIONS`
  has no active retrieval cutover path.
- [ ] Make `corpus_id` a Qdrant tenant index in any shared collection because
  every live search is corpus-scoped; create it before HNSW construction.
- [ ] Represent Fast/Hybrid/Graph eligibility as versioned point payload or
  named-vector policy so one child vector is not stored three times merely to
  expose three retrieval routes.
- [ ] Design a versioned single/multi-collection target with rollback.
- [ ] Benchmark filtering, graph isolation, write amplification, RAM, and recall.
- [ ] Migrate one small corpus through a resumable dual-write/cutover.
- [ ] Produce and approve an ownership manifest, then remove empty/deleted/orphan
  collections and stale shared routing cards through the durable cleanup path.
- [ ] Do not migrate large corpora until exact point/count parity is proven.

### P3.4 Quantization - Experiment

- [x] Measure current vector RAM/disk by collection and representation: Qdrant
  volume about 26 GiB, container 6.63/8 GiB, active vectors/HNSW in memory,
  payload on disk, no quantization.
- [ ] Apply scalar quantization to a cloned small corpus.
- [ ] Evaluate scalar `int8` with quantized vectors in RAM, original vectors on
  disk, rescoring enabled, and multiple `hnsw_ef` values against exact-search
  ground truth.
- [ ] Measure the APFS bind-mount I/O cost of rescoring before adopting an
  on-disk-original design.
- [ ] Evaluate recall@85, nDCG, routed-document coverage, final evidence
  coverage, p50/p95, and RAM; Qdrant latency alone is not the quality gate.
- [ ] Adopt only if quality loss and latency meet agreed thresholds.

Owner directive adopted 2026-07-13: add Qdrant binary quantization as the new
candidate configuration, without bypassing the recall and cutover gates above.

- [ ] Add a versioned binary-quantization collection profile with original-vector
  rescoring and an explicit oversampling policy; apply it to every applicable
  per-corpus vector lane at collection creation.
- [ ] Reconcile existing collections idempotently and verify the effective
  Qdrant collection configuration by readback; do not silently treat an
  unsupported server/client combination as success.
- [ ] Exercise exact/unquantized fallback and quantized+rescore searches in
  automated tests, then measure held-out recall, p50/p95, and memory before
  production-wide adoption.

### P3.5 Reranker Serving Alternatives - Experiment

- [ ] Benchmark current warmed MPS reranker at realistic candidate shapes.
- [ ] Benchmark CPU/int8 ONNX only if the selected model exports correctly.
- [ ] Compare throughput, p50/p95, ranking quality, memory, and contention.
- [ ] Do not switch models or runtimes based on theoretical speed alone.

## Quick Upload And Filesystem Contract

- [x] Quick Upload creates a durable asynchronous batch.
- [x] Uploaded files are authoritative in the host-visible per-corpus drop-off.
- [x] Original filenames are preserved with deterministic collision suffixes.
- [x] Backend and ingest worker share the same drop-off mount.
- [x] Quota checks use Mongo `stored_bytes`; filesystem walk is fallback-only.
- [x] UI reports the relative drop-off path.
- [ ] Decide whether manually placed files should be watched automatically or
  require explicit Folder Sync.
- [ ] If watch mode is added, implement inbox/processing/processed/dead-letter
  states with durable identity and duplicate protection.

## Required Three-Tier Regression Matrix

Run every row for Fast, Hybrid, and Graph unless the row explicitly tests a
tier-specific feature.

- [ ] Expert term direct hit, such as FACS or Laban.
- [ ] Naive vocabulary bridge, such as “make the actor look less stiff.”
- [ ] Focused single-fact question.
- [ ] Broad thematic synthesis.
- [ ] Enumeration/list requiring sibling expansion.
- [ ] Procedure requiring ordered steps.
- [ ] Comparison requiring evidence from multiple documents.
- [ ] Follow-up anchored to a previously accepted document.
- [ ] Negative control with nearby but non-answering vocabulary.
- [ ] Cross-domain within one corpus.
- [ ] Cross-corpus query requiring both corpora.
- [ ] Cross-corpus query where one selected corpus is irrelevant.
- [ ] Corpus with no tree but valid flat vectors.
- [ ] Deleted/non-queryable corpus.
- [ ] Cold-start query.
- [ ] Concurrent query while ingestion/backfill is active.

For every run record:

- [ ] Plan, probes, obligations, and original-query lane.
- [ ] Corpus/document/tree routes and route scores.
- [ ] Vocabulary matches, concept IDs, provenance, and planner expansions.
- [ ] Candidate counts before/after fusion, reranking, and final selection.
- [ ] Corpus/document distribution and reservation reasons.
- [ ] Answerability, lane coverage, evidence coverage, and refusal reason.
- [ ] Embed, vocabulary, tree, retrieval, graph, rerank, hydrate, and total time.
- [ ] Selected source IDs, calibrated scores, and evidence text.

## Definition Of Strict Ready

A corpus is strict-ready only when:

- [ ] Every eligible document is parsed, structured, indexed, and queryable.
- [ ] Qdrant child-vector counts match durable Mongo child counts.
- [ ] Every summary-required parent has one validated, provider-attributed,
  child-evidenced summary.
- [ ] Every document summary tree is synchronized.
- [ ] Vocabulary projection is complete for the selected extraction contract.
- [ ] Required graph extraction is validated and promoted.
- [ ] No durable failed/running/duplicate job remains.
- [ ] No ontology predicate/entity-type violation remains under the active
  contract.
- [ ] Readiness denominators exclude only explicit, documented duplicates or
  structural skips.
- [ ] All three retrieval tiers pass smoke and negative-control probes.

## Implementation Log

### 2026-07-13 - Baseline capture (pre-edit requirement)

- Commit: 4327713
- Owner: goal-mode agent
- Corpus/data scope: all active corpora (read-only)
- Code changes: `backend/scripts/capture_raptor_baseline.py`,
  `backend/scripts/probe_tier_latency.py`
- Before metrics: census reproduces the recorded durable baseline exactly
  (polymath_v2 84,987/67,953; jobs 50,522/500/775; tree 21,432/20,942/23,394).
  Live probes: Fast 42.7s / Hybrid 47.8s / Graph 54.7s / cross-corpus 21.2s.
  Tungsten negative control fails closed. Cross-corpus Hybrid FALSELY refused
  an answerable query that single-corpus answers (P0.3/P0.4 evidence).
- Artifacts: `docs/baselines/BASELINE_2026-07-13.json`,
  `docs/baselines/LATENCY_2026-07-13.json`

### 2026-07-13 - P0.3 corpus-floor calibration (code complete; deploy pending)

- Commit: (this commit)
- Owner: goal-mode agent
- Corpus/data scope: none (retrieval policy code only)
- Code changes: new `services/retriever/reservation_policy.py` (single shared
  calibrated reservation bound + ordering contract); `planned_fusion.py`
  (already-selected corpus candidates must pass the same gate before being
  protected; grounded-filter preservation now picks the corpus's best
  candidate and gates it; structured `corpus_reservation_details` +
  `corpus_floor_candidates_skipped` diagnostics); `ranking_policy.py`
  (corpus-floor eligibility now also enforces the shared calibrated bound on
  raw packet scores with a per-corpus eligibility trace; removed the
  unconditional +0.10 reserve bonus — seat protection is the selection
  reason, never a score; `corpus_floor.skipped` entries now carry reasons).
- Durable migration/backfill: none required.
- Tests by tier: `tests/test_corpus_floor_calibration.py` (7 new: shared-gate
  semantics, skip-reason + eligibility trace, no-bonus seat score,
  strong-corpus coverage, finalist existing-candidate gate, finalist strong
  reserve, grounded-filter gated preservation) + updated
  `test_retrieval_ranking_policy.py` skip contract. Full backend suite:
  2,557 passed (embedder priority-gate test needs the repo `scripts/` tree
  present; environmental, passes with it).
- Deployment image/health: pending final rebuild from main (completion rule:
  boxes flip only after deploy + acceptance re-verification).
- Remaining risks: `facets/final_selector.reserve_corpora` (chat-side floor)
  still uses its own discipline; it is superseded by shelf_reserve in P1.5
  and will adopt the shared gate there.

### 2026-07-13 - P0.1 partial: queue reconcile + legacy provenance (durable)

- Commit: (this commit)
- Owner: goal-mode agent
- Corpus/data scope: summary_jobs (500 rows superseded, all belonging to the
  deleted `authentic_library`); parent_chunks provenance stamping —
  polymath_v2 34,681 / markbuildsbrands 20 / ecommerce 791 legacy abstractive
  summaries stamped `summary_model="legacy_unknown"` after a deterministic
  validation gate (min length, not identical/prefix-copy of parent text,
  length ratio, evidence-token overlap); 2,635 validation failures
  quarantined (`summary=None` + reason) for regeneration via the production
  Ghost A path. Backups: `docs/baselines/p0_1_backups/` (prior field values
  and full prior summary text; untracked).
- Code changes: `backend/scripts/p0_1_summary_integrity.py` (retire-orphan-jobs /
  stamp-legacy / quarantine-regen / residual-report / verify subcommands; all
  dry-run by default; verify exits non-zero on failure).
- Evidence for the stamp decision: sampled placeholder-point parents show the
  Mongo summaries are real abstractive text (0/1000 byte-identical to parent
  text); the Qdrant `summary_model=""` points are overwhelmingly stale
  projections. The Qdrant writer's storage-boundary contract explicitly
  documents `legacy_unknown` as the marker for intentionally imported legacy
  summaries.
- Status: canary corpus (markbuildsbrands) VERIFIED end-to-end (1,009/1,009
  attributed, 0 empty-model points). ecommerce reindex + polymath_v2
  regeneration running as bounded resumable jobs; boxes flip after all
  corpora verify and three-tier recall probes show no regression.

### 2026-07-13 - P0.4 honest answerability (code complete; live verify pending restart)

- Commit: (this commit)
- Owner: goal-mode agent
- Root cause (live baseline evidence): the synthetic fallback probe
  (`query_plan.FALLBACK_PROBE_ID == "primary"`, created for undecomposed
  queries) became a refusal-critical `concept:primary` atom, and
  `filter_grounded_planned_candidates` gated single-lane packs at the 0.75
  grounding threshold despite its documented multi-side contract — together
  producing the recorded cross-corpus false refusal ("did not establish
  primary strongly enough").
- Code changes: fallback probe id is now a documented reserved constant;
  the grounded-lane filter excludes synthetic lanes (named single-side
  filtering unchanged); retriever sufficiency separates lane COVERAGE
  (`selection.lane_coverage`, telemetry incl. synthetic lanes) from
  ANSWERABILITY (undecomposed queries judged by the strict evidence-atom
  gate, so negative controls still fail closed);
  `required_concept_coverage` excludes the synthetic lane so the chat gate
  can no longer promote it to a critical atom; chat gate emits
  `lane_coverage`/`answer_shape`/`coverage_threshold` separately; coverage
  thresholds calibrate by answer shape (broad -0.20, enumeration/comparison
  -0.10, floor 0.40 — shape-keyed, never content-keyed); refusals name the
  nearest retrieved documents and never leak internal lane ids.
- Tests: `tests/test_answerability_honesty.py` (6) + updated
  `test_planned_fusion.py` fixture that had used the now-reserved lane id.
  Full backend suite green: 2,563 passed.
- Deployment/live verification: pending backend restart (deferred so running
  P0.1 backfills are not killed); acceptance re-probe (cross-corpus answer +
  tungsten fail-closed on all tiers) follows the restart.

### 2026-07-13 - P1.1 held-out suite + contamination firewall (baseline run pending)

- Commit: (this commit)
- Owner: goal-mode agent
- Artifacts: `backend/evals/heldout_questions.jsonl` — 56 questions across
  direct/naive/single-fact/broad/list/procedural/comparison/followup/
  negative-control/cross-domain/cross-corpus/irrelevant-corpus shapes and all
  four active corpora, each with expected docs (validated against Mongo),
  expected concepts, and acceptable-alternate notes;
  `backend/scripts/freeze_heldout_eval.py` (structural + ground-truth
  validation, hash freeze); `backend/evals/heldout_hashes.json` (56 frozen
  hashes); `backend/services/eval_firewall.py` (`is_heldout_query` — any
  future attested/generated representation harvesting MUST consult it);
  `backend/scripts/run_heldout_eval.py` (three-tier runner: doc recall,
  concept recall, answerability match, corpus diversity, latency).
- Baseline metric capture: deliberately deferred until P0.1 completes on
  polymath_v2 so the baseline reflects the repaired catalog; runs follow the
  backend restart.

### 2026-07-13 - T-HOOK-2 temporal capture + T-HOOK-3 bibliographic identity

- Commit: (this commit; recovered Claude handoff continued on
  `claude-continuation-20260713`)
- Owner: Codex continuation agent
- Corpus/data scope: T-HOOK-2 UGO canary, 203 parent summaries and both Qdrant
  summary projections; T-HOOK-3 all four active corpora, 681 document rows
  (polymath_v2 498, markbuildsbrands 103, ecommerce 79, UGO 1). Documents-only
  bibliographic writes; no parent/chunk rewrite.
- Code changes: strict `ParentSummaryRecord` temporal/latent rows and Mongo
  writer validation; Ghost A JSON + tagged-rescue capture; deterministic
  repeated-literal span binding; repair/reuse preservation; Qdrant summary
  projection; warn-first nested Mongo schema; deterministic Docling/frontmatter/
  HTML/PDF/DOCX/EPUB bibliographic candidates; atomic persisted date-family
  merge; dry-run-first CAS backfill with collision-proof durable preimages and
  presence-aware restore.
- Durable migration/backfill: UGO summary repair was deterministic only (no
  model calls), then reindexed 203/203 summary points. Full-row UGO backup:
  `/data/ingest-files/backups/summary-capture-20260713/`
  (`203` rows, SHA-256 `edb9b9d6a006700a19f6cc32af6c7138fee437577a80b136de26c7673d0c9def`).
  Bibliographic backups: `/data/ingest-files/backups/bibliographic-20260713/`
  (`498/103/79/1` rows; SHA-256
  `470d17d9f58345f8e07d1c314053085a6e1de9bb35d0e0539d9ce3323310b829`,
  `e766ae1e1360c5fa96c8deb5746bc84eb84b970c7576fd5a81896a940a68ddea`,
  `bca49567364face570695c7951e800082f13937e6e23980898ed731c3670ab27`,
  `02748551c2416922c0a40ecd6d6bd91ff096bcabb419663591710fb52c1515cd`).
  Immediate apply rerun planned 0 rows and created no new backup; all four
  hashes remained unchanged.
- Before metrics: UGO 203 valid summaries but only 47 explicit temporal classes,
  20 nonempty time-expression arrays, and 47 latent rows. Bibliographic coverage:
  polymath_v2 0 dates/0 provenance; mark 0/0; ecommerce 27/79 (unsafe v1 stamp);
  UGO 0/0.
- After metrics: UGO 203/203 explicit bounded classes, 203/203 array-valued
  temporal + latent fields, 0 strict/span/schema violations, 0 changes to the 47
  pre-existing latent rows, and Mongo equals both Qdrant projections 203/203.
  Bibliographic: 681/681 v2 stamps, 86 complete publication-date families,
  0 mixed families, 0 file-time publication dates, 0 unexplained nulls, and
  681/681 rows equal their planned backup postimages.
- Tests by tier: focused S1 integration 121 passed / 1 skipped; focused S2
  capture + migration safety 97 passed / 1 skipped; backend non-integration
  suite 2,782 passed / 4 skipped / 3 deselected plus all five path-relocated
  Mac-sidecar cases rerun green (2,787 product tests total). Execution-plan
  recovery verifier independently proves 533/533 claim parity and 409/409
  checkbox coverage.
- Cross-corpus test: full sorted parent fingerprints stayed byte-identical for
  polymath_v2 130,503, mark 1,015, ecommerce 10,222, and UGO 203 rows (141,943
  total). All four document scopes pass the same date-family/null-reason gates.
- Failure/rollback test: fake-Mongo tests cover CAS collision/abort, backup
  collision/no-op non-truncation, and field-presence restoration. A real
  disposable Mongo drill restored 498/498 rows with 0 preimage mismatches and
  then dropped the drill database.
- Deployment image/health: query backend + offline ingest worker rebuilt with
  base + local override + Apple MLX + offline-ingest overlays; both healthy.
  `verify_backend_runtime.sh` passed with live 1,024-d embedder; Mongo, Qdrant,
  LiteLLM, Redis, reranker, and Neo4j health all green. Updated validators are
  installed `moderate`/`warn` and exact-readback matches source.
- Remaining risks: deterministic language coverage remains 0 because no source
  asserted a language; honest null is retained. Claude's earlier unsafe
  ecommerce v1 pass truncated its original 79-row preimage backup before this
  continuation, so the exact pre-v1 state is not recoverable; the durable v2
  backup starts from post-v1/pre-v2 state, and v2 changed ecommerce provenance
  only (author/title/date coverage did not change). Legacy temporal
  classification beyond the UGO canary remains correctly deferred to T-MAIN
  Phase 3 rather than paid regeneration.
- Checklist boxes closed: T-HOOK-2, T-HOOK-3.

### 2026-07-14 - CP1 / Rebatch Phase A deployment validation (g1-g10)

- Commit: implementation commits `ee78daf` (CP1-D1/D2), `0d898db`
  (CP1-D3), and `e204d55` (CP1-D4); consolidated receipt/handoff in this
  commit on `claude-continuation-20260713`.
- Owner: Codex executor under the owner-activated continuous GO; Claude
  senior supplied/rule-checked the runbook gates and measurement corrections.
- Corpus/data scope: fresh real-API corpus `rebatch_smoke_v2`, API-discovered
  corpus ID `62193743-4175-40da-b861-ba1e1e567b9a`, batch ID
  `fb9271d9-ec89-4614-bd81-991cb07562e0`; five files (2 PDF, 2 Markdown,
  1 transcript-style Markdown). `polymath_v2` remained frozen; no production
  corpus deletion, subset reingest, heading projection repair, or semantic
  layer build occurred.
- Code changes: CP1-D1 routes text-layer PDFs through no-OCR layout parsing
  with a surfaced pypdf font-layout fallback and preserves OCR for image-only
  PDFs; CP1-D2 makes summary-enabled batch completion durable/honest and
  adds Flash-primary provider-pool rejection handling; CP1-D3 applies the
  shared deterministic bibliographic capture hook to all PDF paths; CP1-D4
  captures general qualified temporal-expression families with exact offsets.
  Gate harnesses remained disposable under `/tmp` and were not product code.
- Durable migration/backfill: the two fixture document rows received a
  backup-first deterministic bibliography repair (2/2, SHA-256
  `673055fc215755442f570e1afde2493cf20e155d308a4c10aad2006d1c46ae8d`);
  the smoke corpus's 106 Ghost B rows were backed up before CP1-D4
  re-extraction (SHA-256
  `cbd18d94caea4d7f172e581caadad2f8b1757a196aab1fdd7f144a81d7b18fbd`),
  then recreated as 99 valid RunPod Flash artifacts plus 7 honest skips.
  The senior-authorized deterministic card pass built 5/5 cards with no
  skips/rejections. g10 appended repair audit history only; artifact row
  counts remained unchanged.
- Before metrics: original g1 found digital PDFs on a flat text-layer bypass;
  first fresh-corpus g2 saw 61/80 required summaries and exposed premature
  batch completion/provider rejection; g3 found both fixture authors/dates
  null despite visible title-page labels; g4 initially retained bare years
  instead of qualified date phrases. These were treated as general lane or
  capture-parity defects, never fixture-keyed exceptions.
- After metrics:

  | Gate | Verified deployed result | EXIT |
  |---|---|---:|
  | g1 | 5 documents, 87 parents, 106 children; 0 empty fixture structural heading paths; all five declared Chapter/Part headings present | 0 |
  | g2 | 80/80 required summaries; latent coverage 26/36 (72.22%); temporal class missing 0 | 0 |
  | g3 | 5/5 bibliographically supported or honestly null; Maria Okafor/2019 and Edwin Halvorsen/2004 exact | 0 |
  | g4 | 106/106 extraction rows, 99/99 eligible valid, RunPod Flash provider; `winter 1911` and `2018 drought summer` complete | 0 |
  | g5 | 931 lexicon rows, 913 linked graph entities, 3/3 runtime-discovered Mongo/Qdrant/Neo4j joins exact | 0 |
  | g6 | 5 cards for 5 documents, exactly one each, all `central_subjects` nonempty | 0 |
  | g7 | Mongo-computed equals Qdrant: naive 186, hrag 186, graph 106; Neo4j entities 913 | 0 |
  | g8 | Real authenticated readiness endpoint `fully_enriched`, non-stale, no blockers | 0 |
  | g9 | 9/9 real `/api/chat` SSE cases across Fast/Hybrid/Graph; answerable cases have correct smoke citations; verified-absent case model-skipped/fail-closed on every tier | 0/0/0 |
  | g10 | 18 Mongo artifact deltas all zero; 15 identity surfaces have 0 missing/duplicate groups; Qdrant and Neo4j counts unchanged; readiness remains `fully_enriched` | 0 |

  Exact commands, output-tail numbers, true exits, and `/tmp` log pointers are
  preserved in `COORDINATION.md` entry
  `2026-07-14T10:30:44Z EXECUTOR -> SENIOR :: RECEIPT` and its preceding
  per-gate receipts. All wrappers captured the inner exit before appending
  `EXIT=<n>`; no piped exit status was used.
- Tests by tier: CP1-D1 focused/adjacent adapter coverage 70 passed; D2 suites
  88 + 58 + 124 passed (one opt-in live test deselected); D3 suites 78 + 42
  passed (one opt-in live test deselected); D4 focused + adjacent suites 19 +
  6 passed. Live g9 executed three question shapes on all three retrieval
  tiers with an ephemeral in-memory probe bearer.
- Cross-corpus test: this job intentionally mutated only the fresh smoke
  corpus. Existing production corpora and frozen `polymath_v2` were not
  re-enriched or repaired. CP1 fixes are general and unit-tested with
  synthetic non-fixture examples; the two fixture PDFs served only as e2e
  ground truth.
- Failure/rollback test: all durable backfills were backup-first; PDF parsing
  fallbacks surface counters/reasons; RunPod Flash deployment used
  blue-green endpoints plus synthetic behavioral canaries after in-place
  deployment proved untrustworthy. Old endpoints were deleted only after both
  replacement fleets passed. g10's synchronous repair-cycle restart left
  every artifact/identity/vector/graph count unchanged.
- Deployment image/health: backend and ingest worker rebuilt with the exact
  compose overlays; changed runtime files were hash-identical; runtime
  verifier passed with the live 1,024-dimensional embedder. Both upgraded
  RunPod Flash accounts passed qualified-temporal canaries and were exercised
  by production re-extraction (primary 5 batches, secondary 1).
- Remaining risks: artifact completion for the 19 late g2 rows predates D2
  deployment and occurred via the now-forbidden direct backfill path; the
  durable mechanism is proven by test + canary + CP2. All 19 were Hy3 and
  remain on the explicit CP2 watch list in `COORDINATION.md`. g6 required an
  explicit deterministic post-ingest card pass; automatic-vs-post-pass card
  production is deferred to the Librarian Phase 1 ledger. Flash 1.18
  in-place deploy success is not behavioral proof, so blue-green + canary is
  the standing method. `source_parse_job_plan.changed=true` on idempotent
  same-ID upserts is misleading telemetry reserved for P0.6/P2.7 hygiene.
  One diagnostic emitted encrypted ciphertext to local tool output; no
  plaintext was exposed and no value was persisted, moved, or committed;
  subsequent probes used explicit projections and in-memory bearer tokens.
- Checklist boxes closed: Rebatch Runbook Phase A / BUILDLINE CP1 exit
  (g1-g10), senior-certified in `COORDINATION.md`. Phase B is outside this
  job and remains blocked under its separate owner/glide gate; no Phase B
  work was started by this completion entry.

### 2026-07-14 - T5.6 Qwen3 universal query-instruction A/B (rejected)

- Commit: this commit.
- Owner: owner-approved registry wording; executor ran the pre-authorized A/B;
  senior verified predeploy receipts and authorized deployment.
- Corpus/data scope: read-only held-out retrieval against the existing active
  corpora. Stored document/concept/tree vectors and durable corpus artifacts
  were not written.
- Code changes: query instructions now resolve from the immutable
  `embedding_instruction_registry.v1`; cache and batch-group identity includes
  instruction version; corpus-frozen profile override is supported; document
  and neutral serialization remains raw. The held-out runner gained safe
  suffixed output, and an asserting A/B comparator preregisters the promotion
  gates.
- Durable migration/backfill: none.
- Before metrics: on the 32 Fast IDs reached before early stop,
  baseline_live_v0 mean latency 31.697s; naïve hits 5/5 and recall .740.
- After metrics: universal mean latency 37.381s (+17.9%); naïve recall .800
  but hits fell to 4/5. Decisive row q032 changed from hit=true/recall=.200
  to hit=false/recall=.000. Candidate rejected immediately; Hybrid/Graph not
  run because Fast is the prerequisite gate.
- Tests by tier: Fast partial 32/58, zero runtime errors; one reached negative
  remained fail-closed. Candidate was intentionally interrupted at the first
  absolute failure; the complete 5/5 negative and cross-corpus gates were not
  reached. Built-image unit/adjacent suites passed 40 + 19 before candidate
  deploy; durable-revert image focused suite passed 40.
- Cross-corpus test: not run because Fast failed before cross-corpus rows and
  the contract forbids advancing to later gates after a Fast failure.
- Failure/rollback test: live profile was reverted first via config pin +
  container recreate, then repository/config/compose defaults were restored
  to baseline_live_v0 and redeployed without an override. Profile-versioned
  cache keys prevent cross-arm reuse. No vector rebuild is required.
- Deployment image/health: canonical compose overlays rebuilt/recreated both
  shared-image containers; runtime verifier passed before the A/B and after
  both immediate and durable reverts (live dimension 1024). Final live profile
  is baseline_live_v0 / qwen3-retrieval-query-v1.
- Remaining risks: the universal wording produced several useful positive
  flips but is not safe as a global default. A future attempt needs a new
  registry version and another frozen-suite A/B; this rejected profile must
  not be promoted by configuration drift.
- Checklist boxes closed: none. P2.3 acceptance remains open because the
  candidate did not improve safely. Durable receipt:
  `docs/baselines/T56_QWEN3_UNIVERSAL_AB_2026-07-14.md`.

### 2026-07-14 - T3.1 P2.5b legacy compatibility adapters

- Commit: this commit on `claude-continuation-20260713`.
- Owner: Codex sole executor under `CODEX_MISSION.md`; Claude senior reviews
  the receipt through `COORDINATION.md`.
- Corpus/data scope: read-only compatibility validation over all 696 current
  document rows plus bounded 5,000-row samples from each large legacy family:
  Ghost-B extractions, summarized parent chunks, and corpus lexicon.
- Code changes: added pure, closed Pydantic adapters for document/source
  identity, legacy ERE observation bundles, parent `RetrievalSummary`, and
  lexicon `ConceptSense` identity mappings. Weak/content source identities
  preserve the legacy `doc_id` and surface `needs_owner_lineage`; only strong
  URL/YouTube keys mint logical IDs. Adapter IDs use the frozen
  `logical-artifact` namespace and raw-byte SHA-256 receives the canonical
  algorithm prefix without rehashing.
- Durable migration/backfill: none; adapters are additive/read-only and do not
  rewrite legacy collections.
- Before metrics: no executable legacy-to-envelope compatibility boundary or
  adapter identity goldens existed.
- After metrics: live rows adapted successfully: documents 696/696; Ghost-B
  5,000/5,000 of 264,074; summarized parents 5,000/5,000 of 97,671; lexicon
  5,000/5,000 of 378,366. Zero failures.
- Tests by tier: focused adapter plus hash/identifier goldens 51 passed
  (`EXIT=0`); adjacent semantic-contract suite 62 passed / 3 skipped
  (`EXIT=0`). Tests freeze adapter IDs and cover strong-vs-weak lineage,
  no-promotion recursion, exact temporal offset round trips, malformed-row
  fail-closed behavior, deterministic replay, and input immutability.
- Cross-corpus test: the live census used the shared collections without a
  corpus filter; all current document rows passed and each large family had a
  deterministic bounded sample.
- Failure/rollback test: malformed required identity/nested fields fail with
  the complete missing-path list; unverified temporal offsets remain
  candidate-only with a validation drop and no quote hash. Rollback is code
  removal only because no durable store or request path changed.
- Deployment image/health: no live deployment required; this is an additive,
  currently unreferenced compatibility module. It was copied with tests into
  a disposable container based on the current canonical image.
- Remaining risks: T3.2 writer/validator integration and T3.4 UGO annotate-only
  parity are still required before any envelope-era artifact writes or P2.5b
  acceptance closure.
- Checklist boxes closed: none; mission T3.4 reserves the P2.5b acceptance
  scratch until the UGO canary proves behavioral parity.

### 2026-07-14 - T3.2 envelope validators and typed summary writer boundary

- Commit: this commit on `claude-continuation-20260713`.
- Owner: Codex sole executor under `CODEX_MISSION.md`; Claude confirmed the
  exact (envelope + validators + writer) scope in `COORDINATION.md` at
  2026-07-14T13:06:18Z.
- Corpus/data scope: additive contract deployment against the live Polymath
  Mongo database. No corpus artifact was regenerated, no retrieval path or
  vector changed, and no semantic artifact body was written.
- Code changes: added the strict shared `polymath.artifact_envelope.v1` with
  typed bodies, canonical schema/body hashes, immutable revision identity,
  explicit knowledge status, and lifecycle/validation outside body identity.
  Added `ParentSummaryWrite` plus one central writer used by summary
  generation, ingestion backfill, valid deterministic repair, and tree heal.
  Added closed warn-first validators for `semantic_artifacts`,
  `projection_manifests`, and `projection_outbox` while retaining the five
  existing validators.
- Durable migration/backfill: the eight definitions were attached with
  `validationAction=warn` / `validationLevel=moderate`. The three future
  collections were created empty/dark; no document migration or semantic
  write occurred.
- Before metrics: five legacy validators existed; no executable shared
  envelope or future-collection validators existed; summary writers could
  independently construct raw Mongo update dictionaries.
- After metrics: exact live readback matches all eight checked-in validator
  structures. Existing counts: documents=696, parent_chunks=142092,
  ghost_b_extractions=264074, corpus_lexicon=378366, summary_tree=45397.
  New counts: semantic_artifacts=0, projection_manifests=0,
  projection_outbox=0.
- Tests by tier: summary/writer focused suite 59 passed (`EXIT=0`); envelope,
  validator, manifest, and outbox suite 52 passed (`EXIT=0`); combined focused
  suite 170 passed (`EXIT=0`); the same 170 passed in a disposable canonical
  built-image container (`EXIT=0`). Identity tests include exact schema/body/
  revision goldens and fresh-process replay. An initial writer test attempt
  exposed an incomplete fake lacking `bulk_write`, and the first disposable
  container attempt lacked required test-only config variables; both harness
  defects were corrected and clean full retries are the reported gates.
- Cross-corpus test: read-only live dry run sampled up to 2,000 rows from each
  existing collection with zero proposed-schema violations; the three new
  collections were empty. This contract task does not alter corpus-scoped
  retrieval, so no retrieval A/B applies.
- Failure/rollback test: malformed/untyped summary commands fail before any
  bulk operation; invalid hashes, revision reuse, bare-dict bodies, missing
  knowledge status, naïve datetimes, and extra fields fail closed. Validators
  are warn-first and can be removed/replaced by `collMod`; application and
  exact readback both returned true `EXIT=0`.
- Deployment image/health: canonical compose overlays rebuilt/recreated the
  backend and ingest worker (`EXIT=0`); runtime verifier reports live embedding
  dimension 1024 (`EXIT=0`). SHA-256 parity for all seven changed runtime
  files is exact across host/backend/worker, and both containers import the
  new boundary types (`EXIT=0`).
- Remaining risks: the three semantic collections remain deliberately dark.
  Outbox drain/activation and UGO annotate-only behavioral parity remain T3.3
  and T3.4 work at Track A4; no acceptance claim is made for those boxes.
- Checklist boxes closed: P0.8 typed-model acceptance and the P2.5b strict
  shared-envelope implementation item only.

### 2026-07-14 - T4.1 SemanticDigestV1 portable contract

- Commit: this commit on `claude-continuation-20260713`.
- Owner: owner-delivered gateway contract; Claude ruled the internal
  fully-required conflict in `COORDINATION.md` at 2026-07-14T13:31:30Z.
- Corpus/data scope: none. This is a pure provider-neutral contract and schema
  identity task; no corpus, provider, retrieval, vector, graph, or Mongo data
  was read or written.
- Code changes: added `models/semantic_digest.py` with the owner field sets,
  literals, enums, and strict closed nested records. Per the senior's
  design-of-record ruling, every list is required and callers must send `[]`
  explicitly; the spec now carries a short §3 erratum. No entity, relation,
  claim, evidence-offset, store-projection, or provider-specific field entered
  the digest.
- Durable migration/backfill: none.
- Before metrics: no executable `SemanticDigestV1` or canonical gateway schema
  hash existed. The literal §3 transcription would have exposed only 4 of 12
  root properties as required, conflicting with §2 and Tier 1 strict modes.
- After metrics: all 12 root properties and every nested property are in each
  object schema's `required`; every object is closed with
  `additionalProperties=false`. The Pydantic 2.5.0 schema golden is
  `sha256:ce106660a46ff7799e79399816dd634645e1b906f80905db3460f70787f97c99`.
- Tests by tier: focused digest + hash/envelope suite 44 passed (`EXIT=0`);
  adjacent semantic contract suite 90 passed / 3 skipped (`EXIT=0`). Tests
  freeze exact field sets, all required properties, closed schemas,
  enum/literal failure, strict types, root/nested extra rejection, explicit
  empty arrays, JSON round-trip, exact schema hash, and fresh-process replay.
- Cross-corpus test: not applicable; the new module has no corpus or runtime
  consumer in T4.1.
- Failure/rollback test: missing root/nested arrays, provider/store fields,
  unknown frames/roles/states/schema versions, and wrong scalar/container
  shapes fail closed. One initial test incorrectly expected strict Pydantic to
  reject Python tuple-to-list conversion; it was replaced with the actual JSON
  contract failure (a non-array string), followed by a clean full retry.
- Deployment image/health: no live deploy is warranted for an unreferenced
  pure model. Tests ran under the canonical image's requirements-pinned Python
  3.11 / Pydantic 2.5.0 environment; T4.3 will wire and deploy the gateway.
- Remaining risks: T4.2 semantic validation and T4.3/T4.4 capability,
  repair/dead-letter, provenance/cache, and UGO canary remain open. The owner
  may veto the senior erratum through an `OWNER ::` coordination entry.
- Checklist boxes closed: P2.5c `SemanticDigestV1` contract + schema-hash
  golden only.

### 2026-07-14 - T4.2 SemanticDigest semantic validator

- Commit: this commit on `claude-continuation-20260713`.
- Owner: Codex sole executor under the owner gateway spec §6 and
  `CODEX_MISSION.md`; Claude reviews through `COORDINATION.md`.
- Corpus/data scope: none. Tests load the checked-in immutable owner domain
  registry read-only; no live corpus/provider/store is accessed.
- Code changes: added a pure `semantic_validate()` boundary over one typed
  `SemanticDigestV1` and immutable `SemanticValidationContext`. Errors are
  deterministic and location-indexed. The context binds the supplied parent,
  claim-to-parent ownership, owner domain IDs, externally validated frames,
  claim-grounded mode, and explicit self-reference identities.
- Durable migration/backfill: none.
- Before metrics: structurally valid digest JSON could not be checked for
  foreign/missing claim references, proposal authority, registry membership,
  or motif/frame closure.
- After metrics: every supporting-claim field family checks existence and
  parent ownership; unknown domains are candidate-only; MF01–MF16 is enforced
  for proposals and motif sequences; frames require support; latent concepts
  require support in claim-grounded mode; motifs require at least two eligible
  proposed/externally validated frames; rejected proposals do not authorize a
  motif; self-links and LLM source-observed/validated promotion fail.
- Tests by tier: final focused validator+digest suite 39 passed (`EXIT=0`);
  adjacent registry/envelope/hash/identity/observation/manifest/outbox/adapter
  suite 128 passed / 3 skipped (`EXIT=0`). Each §6 rule has positive and
  negative coverage, including Pydantic-bypass defense for semantic frame and
  proposal-state checks.
- Cross-corpus test: not applicable; context scope is explicit and tested with
  a foreign-parent claim. No retrieval behavior is involved.
- Failure/rollback test: untyped boundaries; invalid/duplicate context; parent
  mismatch; unknown/foreign/self claim links; unknown/non-candidate domains;
  invalid/unsupported/rejected frames; empty grounded latent support; short or
  unclosed motifs; and source-observed/validated proposals all return precise
  errors or reject context construction. Rollback is code removal only.
- Deployment image/health: no live deploy for this unreferenced pure module;
  tests ran under the canonical Python 3.11/Pydantic 2.5.0 image. T4.3 owns
  gateway wiring and deployment.
- Remaining risks: structural/semantic retry, dead-letter non-canonical
  isolation, capability ladder, provenance/cache, and UGO canary remain
  T4.3/T4.4.
- Checklist boxes closed: P2.5c Python semantic-validator item only.

### 2026-07-14 - T4.3 Structured semantic gateway

- Commit: this commit on `claude-continuation-20260713`.
- Owner: Codex sole executor under the owner gateway spec §§4, 5, 7, 8, and
  9; Claude approved the pinned-LiteLLM compatibility detector in
  `COORDINATION.md` at 2026-07-14T13:51:33Z.
- Corpus/data scope: none. Tests use typed in-memory parent packets and fake
  transports/stores. No real provider call, corpus mutation, paid batch,
  canonical artifact write, graph/vector write, or Ghost A cutover occurred.
- Code changes: added the injected `SemanticGateway`, strict config/result/
  provenance models, official LiteLLM capability detection, Tier1 native
  strict-schema and Tier4 JSON-mode paths, explicit Tier2/3 clear-failure
  stubs, one same-mode targeted repair, noncanonical Mongo cache/DLQ stores,
  and deterministic hashes. The existing LiteLLM wrapper gained one reserved
  explicit `response_format` argument so pool extras cannot override the
  gateway contract.
- Durable migration/backfill: none. The cache and DLQ collections are created
  lazily by Mongo on first T4.4 use; no canonical collection is referenced by
  the store implementation.
- Before metrics: structurally or semantically malformed digest output had no
  single bounded provider boundary, repair policy, capability routing,
  replay-safe cache identity, or isolated failure receipt.
- After metrics: one gateway routes the pinned flash model to Tier1 through
  public LiteLLM metadata, fails unknown capability closed to explicitly
  flagged Tier4, reuses the exact same `response_format` for at most one
  targeted repair, revalidates accepted cache rows, and dead-letters a second
  failure without a success/canonical write.
- Tests by tier: focused gateway/provider suite 60 passed (`EXIT=0`); corrected
  adjacent digest/validator/hash/envelope/registry/observation/provider suite
  132 passed / 3 pre-existing Docker-only skips (`EXIT=0`). Black, compileall,
  `git diff --check`, and changed-diff credential-pattern scan all exited 0.
- Cross-corpus test: not applicable; no retrieval or corpus path is active.
  A foreign-parent claim remains rejected by the adjacent semantic-validator
  suite.
- Failure/rollback test: forced unsupported Tier1 and unimplemented Tier2/3
  fail before a provider call; structural and semantic attempt-1 failures get
  exact targeted repair; a second invalid response and transport exception
  write only DLQ state; corrupt cache state is not served; exception details
  and route credentials are absent from persisted/error receipts. Rollback is
  code removal plus the backward-compatible optional wrapper argument.
- Deployment image/health: no live deploy for this not-yet-called module.
  Tests ran under the canonical requirements-pinned backend image. T4.4 owns
  the canonical-image deployment and real UGO provider canary.
- Remaining risks: Tier2/3 are deliberately unimplemented stubs; the real
  provider may expose schema dialect or model-route differences not visible
  to fake transports. T4.4 must prove 10 Tier1 UGO packets, at least one
  repair, a synthetic DLQ, and Tier1/Tier4 shape parity before any cutover.
- Checklist boxes closed: P2.5c targeted repair/dead-letter,
  determinism/provenance/cache, and prompt/schema separation only. The
  combined capability-ladder item and all UGO/downgrade acceptance stay open.

### 2026-07-14 - T4.4 UGO gateway canary and external-provider limit

- Commit: this commit on `claude-continuation-20260713`.
- Owner: Codex sole executor under `CODEX_MISSION.md`; Claude approved the
  probe ladder, three versioned repair iterations, Tier3 forced-tool lever,
  final close-either-way run, and CP9 provider preflight through
  `COORDINATION.md`.
- Corpus/data scope: read-only discovery of `Unspoken Global Outcomes`, corpus
  `bcf80054-7611-47d0-ae16-fa7fed259b13` (1 document, 203 eligible parents),
  with 10 evenly spaced accepted-evidence parent packets. Provider results
  were written only to the noncanonical semantic cache/DLQ. No paid batch,
  canonical semantic artifact, vector/graph write, or Ghost A cutover ran.
- Code changes: added a strict versioned structured-output route registry and
  sanitized capability probe; extended the gateway with an exact forced
  `submit_semantic_digest(SemanticDigestV1)` Tier3 path; versioned the compact
  initial prompt as `parent-digest.v5` and root-only Tier3 repair as
  `parent-digest-repair.v2`; added repair identity/hash provenance and cache
  identity; and added the UGO canary plus a tiny closed-tool LongCat probe.
- Durable migration/backfill: no semantic migration/backfill. Live test rows
  are explicitly noncanonical: four accepted cache rows across the two Tier3
  investigations and four evidenced DLQs; all have `canonical_write=false`.
  Before the final worker restart, a senior-authorized backup-first fence
  superseded 10,000 dormant ecom extraction rows from the killed 2026-07-13
  chain with reason `parked_pending_owner_ecom_cp9_2026-07-14`. The sorted
  10,000-row JSONL backup is local/ignored, SHA-256 receipted, and reversible;
  no extraction, deletion, reingest, or canonical projection occurred.
- Before metrics: LiteLLM metadata claimed flash response-schema support, but
  no configured route had a runtime receipt and the UGO digest contract had
  never been exercised against a real provider.
- After metrics: native strict-schema probe accepted 0/5 routes. All four
  DeepSeek routes returned HTTP 400 `response_format` unavailable; LongCat
  returned HTTP 200 but no valid closed JSON. Flash Tier4 remained
  structurally unreliable. Flash Tier3 produced 3 accepted noncanonical rows
  before one packet exhausted two attempts on three unknown-domain assignment
  errors; the other six calls were cancelled before persistence. The final
  repair arguments had exactly the 12 root fields, proving the wrapper fix,
  but not semantic correction reliability. LongCat's tiny forced-tool probe
  returned exact closed `{ok:boolean}` arguments in 3493.55 ms; a full digest
  remains unverified. Both routes therefore have `verified_digest_path=null`.
- Tests by tier: final exact focused suite 53 passed (`EXIT=0`); adjacent
  contract/regression superset 212 passed / 3 pre-existing Docker-only skips
  (`EXIT=0`); Black 9/9, compileall, `git diff --check`, sanitized JSON parse,
  and filename-only credential-prefix scan all passed. The earlier 57-focused
  and 168-adjacent repair-v2 gates also passed before the final provider run.
- Cross-corpus test: not applicable to this one-corpus acceptance canary. The
  pre/post canonical census covered every live Qdrant collection plus global
  Neo4j and Mongo semantic-artifact counts; all four snapshots were exact.
- Failure/rollback test: real provider failures exhausted at most two attempts
  and wrote only hashed/raw-body-free DLQ state. Final census stayed Mongo
  semantic artifacts=0, Qdrant points=1,364,767, Neo4j nodes=1,361,818, and
  Neo4j relationships=3,712,432. No synthetic acceptance was substituted.
  Rollback is removal of the unconsumed gateway additions and noncanonical
  cache/DLQ rows; canonical retrieval state requires no rollback.
- Deployment image/health: after the authorized ecom queue fence, the exact
  three-overlay command rebuilt/recreated backend and ingest-worker from the
  final tree. Runtime verification returned a live 1024-dimensional embedding.
  Seven gateway/registry/probe files hash identically across both containers;
  both load the final flash/LongCat null-path verdict and prompt v5/repair-v2.
  Post-restart ingest batches active=0, extraction jobs running=0, ecom active
  extraction=0, and exact fenced reason count=10,000.
- Remaining risks: no full provider-verified digest path exists. CP9 preflight
  must retest flash native strict schema and run the same 10-packet Tier3
  acceptance on candidate LongCat; whichever passes becomes the digest route.
  If both fail, the paid digest pass parks for an owner line. Tier2 local
  grammar fallback and the Tier1/Tier4 parity acceptance remain open.
- Artifacts: `docs/baselines/T4_4_STRUCTURED_CAPABILITY_PROBE_2026-07-14.json`,
  `docs/baselines/T4_4_LONGCAT_TIER3_PROBE_2026-07-14.json`, and
  `docs/baselines/T4_4_UGO_GATEWAY_EXTERNAL_LIMIT_2026-07-14.json`, plus
  `docs/baselines/T4_4_ECOM_QUEUE_FENCE_2026-07-14.json`.
- Checklist boxes closed: P2.5c cutover-stays-gated only. Capability-ladder,
  10-packet Tier1, synthetic DLQ demonstration, and Tier1/Tier4 parity remain
  open without redefining passing.

### 2026-07-14 - T8.1 strict LocalExtractionV1 and spaCy observation boundary

- Commit: this commit on `claude-continuation-20260713`.
- Owner: Codex sole executor under `CODEX_MISSION.md`; Claude certified the
  owner-verbatim boundary, conservative normalization seed, real-UGO audit,
  unresolved-mass interpretation, and publication through `COORDINATION.md`.
- Corpus/data scope: read-only, evenly spaced 20-child sample from all 659
  source-child rows in `Unspoken Global Outcomes`, corpus
  `bcf80054-7611-47d0-ae16-fa7fed259b13`. Temporary exact-field source text
  stayed under `/tmp`; the committed receipt contains no raw text or child IDs.
- Code changes: added strict owner-field-exact `EntityMention`,
  `PredicateMention`, `RelationCandidate`, and `LocalExtractionV1` models using
  the exact 25/17/6/2 vocabulary literals; added a 23-lemma conservative
  predicate-normalization v1 registry and strict loader; added deterministic
  spaCy-to-local-extraction compilation with registry/recipe hash provenance,
  matched-count receipts, and unknown predicates routed to
  `unresolved_spans`. Entity/relation arrays remain empty in T8.1: no GLiNER,
  GLiREL, or generic relation is fabricated. Exact-coordinate artifact models
  now preserve boundary whitespace so quotes continue to hash and round-trip.
- Durable migration/backfill: none. This task made no annotation, canonical,
  vector, graph, retrieval, or provider write. The normalization authority is
  explicitly `executor-proposed, owner-ratifiable`; owner ratification remains
  required before promotion.
- Before metrics: the repository had a provider-neutral `ObservationBundle`
  and partial spaCy observations, but no full owner-field-exact
  `LocalExtractionV1`, no strict predicate normalization registry, and no
  count-only real-corpus compiler audit.
- After metrics: 20 UGO children yielded 303 sentences and 374 observed
  predicates. Nine mapped conservatively (INFLUENCES 4, ASSOCIATED_WITH 2,
  COMPARES_AGAINST 2, MEASURES 1); 365 remained unresolved, rate
  0.9759358288770054. Evidence round-trip errors=0, annotation writes=0, and
  provider calls=0. The high unresolved rate is a finding, not a weakened or
  failed acceptance criterion.
- Tests by tier: owner-model gate 20 passed; registry/model gate 33 passed;
  compiler unit gate 39 passed; exact-coordinate focused gate 42 passed / 7
  known no-spaCy skips; adjacent contract gate 157 passed / 7 known no-spaCy
  skips. After formatting, the trained-spaCy 3.8.14 / `en_core_web_sm` 3.8.0
  suite passed 49/49 (`EXIT=0`). Final static gate passed Black 9/9,
  compileall, both JSON parses, `git diff --check`, and changed-filename secret
  scan (`EXIT=0`). Earlier failed attempts and their true exits remain logged.
- Cross-corpus test: not applicable to this UGO-only annotate/read-only task.
  T8.4 owns the full canary census; later pair work retains the cross-corpus
  gate.
- Failure/rollback test: strict input rejected Mongo's extra `_id`; the input
  was projected to the exact three audit fields rather than relaxing the
  boundary. Unknown lemmas stayed unresolved rather than becoming
  `ASSOCIATED_WITH`. A real boundary-whitespace quote exposed inherited string
  stripping; focused and adjacent tests prove the narrow fix. Rollback is code
  and registry removal; no persisted semantic or retrieval state exists.
- Deployment image/health: no live deployment for this unreferenced pure
  model/compiler slice. Canonical-container tests used Docker-copied files
  because tests are not baked, while the real trained-parser gate used the
  existing pinned local spaCy environment. No endpoint or worker behavior was
  activated.
- Remaining risks: 97.5936% of observed predicate lemmas are intentionally
  unresolved; copular/evidential predicates need dependency-, modality-, or
  signature-aware treatment rather than ambiguous seed expansion. T8.2 must
  carry unresolved predicate surface forms forward as observation-only/untyped
  candidates, never discard or coerce them. Predicate-normalization v1 still
  needs owner ratification; empty RESULTS_IN/APPLIES_UNDER/PART_OF/USED_FOR
  seed rows remain intentional pending dependency rules.
- Artifact: `docs/baselines/T8_1_LOCAL_EXTRACTION_UGO_AUDIT_2026-07-14.json`.
- Checklist boxes closed: none. T8.1 is complete, but P2.5a's deterministic
  pilot and P2.5b's every-provider ObservationBundle items remain open until
  their later integration and acceptance gates.

### 2026-07-14 - T8.2 deterministic ClaimRecordV1 compiler

- Commit: this commit on `claude-continuation-20260713`.
- Owner: Codex sole executor under `CODEX_MISSION.md`; Claude approved the
  bounded field design with authority, projection, link-provenance, scope,
  and accounting conditions, then certified the real-UGO receipt through
  `COORDINATION.md`.
- Corpus/data scope: read-only, same evenly spaced 20-child sample from all 659
  source-child rows in `Unspoken Global Outcomes`, corpus
  `bcf80054-7611-47d0-ae16-fa7fed259b13`. Temporary exact-field source text
  stayed under `/tmp`; the committed receipt contains no raw text or child IDs.
- Code changes: added strict candidate-only `ClaimArgumentV1`,
  `ClaimRecordV1`, `ClaimLinkV1`, and `ClaimCompilationV1` contracts; their
  concrete engineering field sets are explicitly
  `executor-proposed, owner-ratifiable`, owner ratification is required, and
  changes require a new schema version. Added a dual-input compiler that
  fail-closes ObservationBundle/LocalExtraction child, evidence, coordinate,
  predicate, and active-normalization closure; preserves every unresolved
  predicate as an untyped observation-only claim surface; binds GLiNER IDs
  only on unique deterministic span containment; and attaches GLiREL relation
  IDs only when source/predicate/target direction agrees with spaCy arguments.
  Explicit-connective-only `RESULTS_IN` artifacts are separate candidate
  claim-to-claim links carrying the connective and rule ID. Domains and frames
  remain T9.1/T9.2.
- Durable migration/backfill: none. No annotation, canonical, vector, graph,
  retrieval, domain/frame, or provider write occurred. The compiler and audit
  are not referenced by a live endpoint or worker.
- Before metrics: T8.1 accounted for 374 predicate observations but emitted
  only nine controlled PredicateMentions; 365 unresolved predicate surfaces
  had no claim-era carrier, and no deterministic ClaimRecord-to-ClaimAssertion
  projection or claim-link accounting existed.
- After metrics: the same 20 UGO children and 303 sentences produced 374 claim
  records from 374 predicate observations (yield 1.0): typed=9/9,
  untyped=365/365, skipped typed=0, carry-forward errors=0. Exact evidence and
  ClaimRecord-to-ClaimAssertion reverse errors were both zero. One explicit
  result-phrase ClaimLink was emitted; one cross-sentence explicit-connective
  candidate was rejected for missing endpoint continuity; one repeated
  same-sentence semantic occurrence was preserved as two observation-bound
  candidate IDs and counted. GLiREL agree/conflict was honestly 0/0 because
  the T8.1 lane contains no GLiREL observations.
- Tests by tier: final focused identity-rider gate 11 passed / 3 disclosed
  no-spaCy skips (`EXIT=0`); post-format pinned-spaCy adjacent gate 52/52
  (`EXIT=0`); canonical adjacent retry 127 passed / 10 disclosed trained-spaCy
  skips (`EXIT=0`) after the documented tests-not-baked docker-cp step. Final
  static gate passed Black 5/5, compileall, exact baseline/run comparison,
  JSON parse, `git diff --check`, and both secret scans (`EXIT=0`). Earlier
  failed wrappers and true exits remain in the coordination log.
- Cross-corpus test: not applicable to this UGO-only read-only compiler audit.
  T8.4 owns the full annotate-only canary census and later PoC work retains the
  cross-corpus gate.
- Failure/rollback test: the first real audit rejected duplicate claim IDs
  when two same-sentence predicate observations shared semantic/evidence/scope
  identity. No row was deduplicated or dropped: the deterministic observation
  ID was added only to the candidate signature, compiler recipe advanced to
  `claim_compiler.v2`, multiplicity became a receipt count, and the projection
  body golden was disclosed and re-frozen as
  `sha256:320f76c2c30cbcbff32a741163ba631ac3f8fc527f351c0549bb29ae006793ec`.
  Synthetic relation-direction conflict remains counted observation-only.
  Rollback is code removal; no persisted semantic/retrieval state exists.
- Deployment image/health: no live deployment for this unreferenced pure
  model/compiler slice. Canonical-image tests used Docker-copied files/tests;
  the real parser and UGO audit used the existing pinned local spaCy 3.8.14 /
  `en_core_web_sm` 3.8.0 environment.
- Remaining risks: 365/374 claim records remain deliberately untyped pending
  dependency/signature work; 73 unresolved-coreference observations across
  303 sentences (~24%) quantify the future ClaimRepair backlog and are not
  guessed here. T8.3 owns richer negation and typed-signature semantics;
  T8.5 owns the GLiREL WITH/WITHOUT compiled-claim decision. ClaimRecord v1 and
  predicate-normalization v1 still require owner ratification before
  promotion.
- Artifact: `docs/baselines/T8_2_CLAIM_COMPILER_UGO_AUDIT_2026-07-14.json`.
- Checklist boxes closed: none. T8.2 is complete, but P2.5a's full
  deterministic pilot and P2.5b's cross-provider/accepted-artifact integration
  remain open until their later canary, promotion, and projection gates.

### 2026-07-14 - T8.3 negation and typed-signature compiler assessments

- Commit: this commit on `claude-continuation-20260713`; the independently
  rollbackable legacy token-preservation fix is `aeec419`.
- Owner: Codex sole executor under `CODEX_MISSION.md`; Claude approved the
  additive sidecar design, ruled that the legacy live remap stays unchanged,
  narrowed the evidence-token boundary after the measured pause, and accepted
  the zero-flip census through `COORDINATION.md`. The concrete assessment
  fields remain `executor-proposed, owner-ratifiable` and require a new schema
  version for changes.
- Corpus/data scope: deterministic synthetic compiler fixtures plus a read-only
  aggregate census of all 659 UGO and 69 `rebatch_smoke_20260714` extraction
  rows. The census covered 39,166 evidence pairs (38,749 entity, zero fact,
  417 relation) without emitting text, evidence phrases, entity names,
  credentials, or artifact IDs. T8.4 owns the full local-assessment UGO run.
- Code changes: added strict versioned claim-negation and relation-semantic
  assessment sidecars beside the frozen `LocalExtractionV1`/`ClaimRecordV1`
  field sets. They preserve exact attached negation cues, quote hashes, and
  only referenced sentence boundaries; compare predicate/cue/compiled-claim
  polarity; retain dependency conflicts observation-only; and keep negated or
  conflicted promotion explicitly owner-pending. Typed signatures reuse the
  existing `ghost_b.DOMAIN_RANGE_MAP` through exact-safe adapters only
  (`CAUSES -> causes`, `PART_OF -> part_of`); ambiguous predicates or entity
  types emit `signature_valid=null` plus reason. No relation is dropped,
  remapped, accepted, or promoted. Count-only receipts are keyed by corpus,
  provider, model, engine, and predicate.
- Durable migration/backfill: none. No Mongo, Qdrant, Neo4j, canonical
  artifact, graph, vector, provider, or retrieval write occurred. The legacy
  `_apply_domain_range` remap was intentionally not changed; its eventual
  annotation-first demotion stays co-scheduled with the P2.5/T-MAIN seam.
- Before metrics: legacy evidence overlap discarded `not`/`no`/`never`; no
  versioned relation assessment carried exact cue/boundary/polarity agreement;
  no compiler contract exposed annotate-only typed-signature validity; and
  the size of the dormant live-remap workload was unknown.
- After metrics: the narrowed token-preservation policy changed zero of
  39,166 stored acceptance decisions (legacy=current for 38,749 entities and
  417 relations; fact lane honestly empty). The migration census found 396
  `DOMAIN_RANGE_MAP`-assessable relations and 112 would-violations (28.28%):
  UGO 85/361 (23.55%), smoke 27/35 (77.14%); stored remap/warn counters and
  statuses were zero because both corpora use the RunPod lane. Synthetic
  sidecar coverage proves valid=false+reason, unsupported=null+reason,
  attached polarity conflict accounting, and claim/relation conservation.
- Tests by tier: token-preservation suite 10 passed / 75 deselected
  (`EXIT=0`); sidecar focused compiler/observation gate 22 passed / 10
  disclosed no-trained-spaCy skips (`EXIT=0`). The broad adjacent compose
  gate reached 223 passed / 10 skips with four pre-existing provider-card
  assertion failures: those tests require DeepSeek v4 Flash `json_schema`
  while the live-verified card intentionally pins `json_object`; no T8.3 file
  touches that seam. The identical four failures reproduced at pre-T8.3
  commit `0d82515`; the separately authorized test-only reconciliation then
  aligned V4 assertions to `json_object`, retained generic schema-rejection
  coverage on the non-V4 card, and made the exact adjacent surface green at
  227 passed / 10 skips (`EXIT=0`). Static and publication gates are recorded
  in `COORDINATION.md`.
- Cross-corpus test: the aggregate no-write census covered both UGO and the
  independently ingested smoke corpus with provider/model/run identities.
  Full PoC-pair semantic assessment remains deferred to the one paid pass.
- Failure/rollback test: the first sentence-bag parity draft flipped 112/417
  relation decisions (26.86%) and was stopped, decomposed, and removed before
  publication. The contract-honest token-only replacement flips 0/39,166 and
  lives in its own commit. Invalid/missing signature mappings fail closed to
  annotated null, while dependency/polarity conflicts remain conserved
  candidates. Sidecar rollback is code removal; no persisted state exists.
- Deployment image/health: no deployment for this unreferenced pure
  assessment slice. Host-checkout gates used the canonical image/compose
  dependencies with read-only mounts. RunPod model pinning and predicate
  offset/polarity/parser wire propagation remain open and require the standard
  blue-green synthetic canary.
- Remaining risks: the reviewed accepted-real-edge compatibility table,
  provider/model/corpus/predicate violation-rate census, false-positive review,
  hard/soft enforcement, negated-relation promotion policy, trained spaCy
  RunPod pin, and wire propagation all remain open. The legacy shared evidence
  gate cannot infer polarity because it lacks predicate offsets, claim
  polarity, sentence identity, and a parser; this is explicitly recorded as a
  P2.5-seam prerequisite. Assessment and predicate-normalization contracts
  still require owner ratification before promotion.
- Artifact: `docs/baselines/T8_3_NEGATION_SIGNATURE_CENSUS_2026-07-14.json`.
- Checklist boxes closed: P2.4 token preservation, relation assessment
  `negated` plus evidence-sentence boundaries, emitted-evidence-only parsing;
  P2.5 annotate-only `signature_valid` plus reason with no drop/remap. Parent
  sections remain open for the risks listed above.

### 2026-07-14 - T8.3 provider-card test-debt reconciliation

- Commit: this rollback-isolated follow-up commit.
- Owner: Codex sole executor; senior-authorized and certified in
  `COORDINATION.md` after the pre-existence proof.
- Corpus/data scope: no corpus data; test contracts only.
- Code changes: updated DeepSeek v4 Flash selection and payload assertions to
  the live-verified versioned `json_object` provider card. The two generic
  JSON-schema rejection tests now use the existing schema-capable non-V4 card,
  preserving retry and lane-downgrade coverage. Production files changed: 0.
- Durable migration/backfill: none.
- Before metrics: the exact adjacent surface was 223 passed / 10 skips / 4
  failures at both current state and pre-T8.3 commit `0d82515` because the
  four tests asserted the live-falsified V4 `json_schema` contract.
- After metrics: the four targeted tests pass 4/4; the exact adjacent surface
  passes 227 / 227 with 10 disclosed trained-spaCy skips, true `EXIT=0`.
- Tests by tier: targeted receipt
  `/tmp/t83_provider_debt_reconcile_targeted.log`; adjacent receipt
  `/tmp/t83_provider_debt_reconcile_adjacent.log`.
- Cross-corpus test: not applicable; no corpus behavior or data path changed.
- Failure/rollback test: pre-T8.3 detached receipt
  `/tmp/t83_preexist_provider_debt.log` proves the identical four failures;
  reverting this commit restores only stale tests, not provider behavior.
- Deployment image/health: no deploy; canonical three-overlay compose with a
  read-only backend bind mount supplied the deployed dependency environment.
- Remaining risks: none for this closed test debt; provider capability
  promotion remains governed by live canaries, never stale metadata.
- Checklist boxes closed: none; this reconciles receipts rather than changing
  product acceptance criteria.

### 2026-07-14 - T8.4 full UGO candidate-claim assessment census

- Commit: this commit on `claude-continuation-20260713`.
- Owner: Codex sole executor under `CODEX_MISSION.md`; Claude accepted the
  full-population findings and ruled that T8.4 closes at census,
  byte-determinism, and publication with zero durable annotation rows.
- Corpus/data scope: all 659 nonempty rows in the sanitized UGO child-text
  projection, not the prior 20-row sample. The source projection is frozen by
  `sha256:180be6adbaf3df18fc42ff68d1e7af84d1abdb202d9eae11a160b56145dfed50`;
  raw text and child IDs remain outside the committed receipt.
- Code changes: added a read-only, count-only census driver that runs the
  pinned trained-spaCy observation compiler, deterministic ClaimRecord
  compiler, and additive T8.3 negation/signature assessment sidecar over an
  exact expected row count. It verifies conservation, candidate-only state,
  evidence/polarity closure, signature accounting, and stable recipe/schema/
  provenance identities; it cannot silently sample or truncate.
- Durable migration/backfill: none. Annotation, persistence, provider,
  promotion, Mongo, Qdrant, Neo4j, graph, and vector writes are all zero.
  Persistence is deferred to CP9 activation or its own separately gated step.
- Before metrics: the 20-row T8.2 sample had 303 sentences, 374 observed
  predicates, 374 candidate claims (9 typed / 365 untyped), one explicit
  result link, 73 unresolved-coreference observations, and no population
  negation census.
- After metrics: 659 rows contain 10,014 sentences and 14,117 observed
  predicates. They produce 14,090 conserved candidate claims (495 typed,
  13,595 untyped) plus 27 fully accounted typed skips for missing subjects,
  a 99.8087% claim yield. The compiler emits 82 explicit-result links;
  173/173 cross-sentence candidates remain conservatively rejected; 3,026
  unresolved-coreference observations quantify the ClaimRepair backlog.
  Negation is 526/14,090 claims (3.7331%): 510 qualifier-only and 16 predicate
  plus qualifier, so 96.9582% of negated claims are qualifier-scoped. Every
  evidence, conservation, polarity, candidate-status, and receipt-accounting
  error is zero.
- Tests by tier: full population `EXIT=0`
  (`/tmp/t84_ugo_full_census_final.log`); independent full rerun byte-identical
  with receipt hash
  `sha256:cb312b6fd45144d82da676aa02db17e75b0d1faac18c7a1f72ca9adce6188699`
  (`/tmp/t84_ugo_determinism.log`); driver compile/Black, frozen-byte match,
  JSON invariants, sanitization, secret scan, and whitespace gate `EXIT=0`
  (`/tmp/t84_static_sanitization.log`).
- Cross-corpus test: not applicable to the mission's UGO population canary.
  The T8.3 migration census already covered UGO plus the independent smoke
  corpus; the later one-paid-pass work retains the PoC-pair requirement.
- Failure/rollback test: declaring 658 expected rows against the 659-row input
  fails before parser work or report creation (`/tmp/t84_fail_closed.log`),
  proving the run cannot game population success by truncation. Rollback is
  removal of the driver and count-only receipt; persisted state does not exist.
- Deployment image/health: the canonical backend image correctly lacks spaCy
  and was not mutated. The census used the same certified pinned parser
  environment as T8.1/T8.2: Python 3.11.15, spaCy 3.8.14, and
  `en_core_web_sm` 3.8.0. No endpoint was deployed or restarted.
- Remaining risks: the deterministic local lane emitted zero relation
  candidates, so dependency/polarity relation counts and typed-signature rates
  are honestly empty, not zero-quality claims. T8.5 introduces controlled-
  label GLiREL observations and owns the first real signature/compiled-claim
  comparison. The 13,595 untyped claims, 3,026 coreference observations, 27
  subject-missing skips, owner-ratifiable schemas, and RunPod parser/wire pin
  remain explicit downstream work.
- Artifact:
  `docs/baselines/T8_4_CLAIM_ASSESSMENT_UGO_CENSUS_2026-07-14.json`.
- Checklist boxes closed: none. T8.4 closes a mission population gate without
  pretending the empty relation lane satisfies P2.5's real-edge violation-rate
  or P2.5a's relation-integrated pilot requirements.

### 2026-07-14 - T8.5 C2 controlled-label GLiREL compiled-claim verdict

- Commit: frozen preregistration published before inference as `0165254`;
  benchmark harness, rejected-with-evidence report, and cursor closure are in
  this commit on `claude-continuation-20260713`.
- Owner: Codex sole executor under `CODEX_MISSION.md`; Claude confirmed the
  no-crosswalk evaluation surface, accepted the concrete thin-evidence floor,
  accepted the gate hash before outcomes, and reviewed the verbatim
  `without_wins` result through `COORDINATION.md`. Owner-approved C2 governs;
  this negative result does not ratify the executor-proposed ClaimRecord or
  predicate-normalization schemas.
- Corpus/data scope: frozen
  `backend/evals/semantic_extraction_gold_v1.json` (9 samples, 29 gold entity
  spans, 9 legacy relations, 12 gold claims), sha256
  `866d1b2104c7bc7d3a5696462058053a6bb041c8bd6784eae59a1f390d5e7816`.
  The legitimate decision base was 6 typed compiled gold claims across 6
  samples and 4 controlled predicate types, clearing preregistered minima
  5/4/3. Legacy relation labels were never mapped into owner PredicateType.
- Code changes: added an immutable versioned gate spec, a read-only local
  benchmark, and four contract tests. WITH and WITHOUT share identical spaCy
  observations, GLiNER mentions, typed predicates, and compiler inputs; WITH
  adds only exact-controlled-label GLiREL candidates. The binder resolves a
  candidate only to a unique endpoint pair, evidence sentence, and unique
  same-label typed predicate; dependency direction remains independently
  enforced by the certified ClaimRecord compiler. Oracle spans, exact legacy
  span score, and untyped-endpoint agreement are explicitly diagnostic-only.
  Product extraction and compiler files changed: zero.
- Durable migration/backfill: none. Provider calls, persistence, promotion,
  Mongo, Qdrant, Neo4j, graph, and vector writes are all zero. Candidate and
  observation permissions remain unchanged.
- Before metrics: the 2026-07-13 open-label baseline produced relation F1
  0.1739 with oracle entities and 0.1333 for GLiNER→GLiREL. The owner's
  controlled-label/compiled-claim hypothesis had not been tested, and T8.4's
  deterministic population lane emitted no relation candidates.
- After metrics: production-shaped GLiNER selected 25/25 controlled mentions.
  GLiREL emitted 4 proposals (`SIGNALS` 3, `PART_OF` 1): 2 bound to typed
  predicates, 1 had ambiguous endpoints, and 1 had no same-label typed
  predicate. The compiler rejected both bound proposals for dependency
  direction, so accepted support remained 0/6: WITH F1 0.0 and precision 0.0
  versus WITHOUT F1 0.0 and the frozen precision minimum 0.50. The oracle-span
  arm emitted 8 proposals, bound 2, and likewise accepted zero. Core claim
  material and quality are exactly equal WITH/WITHOUT; evidence,
  conservation, references, controlled-label violations, and accepted
  label/predicate conflicts are all zero. One proposal agreed with an untyped
  claim's endpoints; it is retained as a no-weight future hypothesis only.
  Verdict: `without_wins`; disposition: `relations_remain_observation_only`.
- Tests by tier: gate freeze verification `EXIT=0`
  (`/tmp/t85_gate_freeze.log`); focused plus adjacent extraction/claim
  contracts 52 passed, `EXIT=0` (`/tmp/t85_adjacent_contracts.log`); two fresh
  full benchmark runs are byte-identical at
  `sha256:e25b48d4725367dfd059f1bc80bc9d138cda23c62c553ab7b787321296c2c33a`
  (`/tmp/t85_decisive_determinism.log`); compile/Black, schema/hash,
  zero-write/invariant, raw-text/identifier, absolute-path, credential, and
  whitespace sanitization gate `EXIT=0` (`/tmp/t85_static_sanitization.log`).
- Cross-corpus test: not applicable to C2's owner-specified frozen gold
  fixture. The decision uses a production-shaped GLiNER arm and a separate
  oracle-span diagnostic arm; neither is allowed to borrow legacy gold labels
  through an eval adapter.
- Failure/rollback test: the harness twice failed closed before model inference
  while its ID namespace and compact-label-hash recipes disagreed with frozen
  contracts; the corrections conformed the harness to existing taxonomy and
  the already-published gate rather than changing either authority. Reversing
  a controlled relation in the contract test binds it but makes the compiler
  reject it, proving dependency direction owns acceptance. Rollback removes
  only the harness/tests/report; no durable state exists.
- Deployment image/health: no deployment or restart. The benchmark ran fully
  offline on the pinned local environment: Python 3.11.15, spaCy 3.8.14,
  `en_core_web_sm` 3.8.0, GLiNER 0.2.26 revision
  `40ec419335d09393f298636f471328b722c6da9e`, and GLiREL 1.2.1 checkpoint
  weights sha256 `05849c34aa6910b6cdcc37ceaab023272c5c9dcccb5a9eb9e94e3ad447200c31`
  on MPS. Exact config, model, trained-label, and inference-label hashes live
  in the frozen artifact.
- Remaining risks: the installed transformers stack reports its known
  DeBERTa tokenizer-regex limitation, and GLiNER's used batch API is deprecated;
  neither frozen config was altered after outcomes. The combined open-label
  and controlled-label evidence closes GLiREL as current Stage-4 owner; no
  re-litigation occurs without an owner-initiated genuinely new evidence class.
  CP9 consumes compiled claims, not GLiREL relations. Typed coverage grows
  through deterministic dependency patterns and a future owner-ratified
  predicate registry, while the single untyped-endpoint observation has no
  current acceptance or promotion power.
- Artifact:
  `docs/baselines/GLIREL_C2_CLAIM_COMPILER_2026-07-14.json`; preregistered gate
  `backend/evals/glirel_claim_compiler_c2_gate_v1.json` at sha256
  `6e0502d6352786286a583d0943fe083a8abaf1feb506ee4bd31b14d6ddef6de9`.
- Checklist boxes closed: C2 GLiREL Stage-4 re-benchmark, as
  **rejected-with-evidence**, not adopted. Relations remain observation-only.

### 2026-07-14 - T9.2 role-bound frame candidates and strict motif matcher

- Commit: this commit on `claude-continuation-20260713`.
- Owner: Codex sole executor under `CODEX_MISSION.md`; Claude ruled the v1
  role, sequence, threading, disposition, M12 qualifier, and coverage
  boundaries through `COORDINATION.md`. The two new recipe registries remain
  executor-proposed, owner-ratifiable.
- Corpus/data scope: deterministic synthetic contract fixtures only. No live
  corpus text, provider payload, or durable semantic row was read or written.
- Code changes: added strict candidate-only frame/motif models; a side-effect-
  free compiler that losslessly binds every lawful ClaimArgumentV1 through an
  exact caller-supplied thread key; and a strict contiguous motif matcher over
  the approved dominant/admissible stage bindings. Added fail-closed versioned
  policies and loader validation. Sequence alignment and role continuity stay
  separate; no fused score or accepted-state path exists.
- Durable migration/backfill: none. Provider calls, spend, persistence,
  promotion, Mongo, Qdrant, Neo4j, projection, and retrieval activation are
  all zero.
- Before metrics: T9.1 could route controlled predicates to 8/16 superframes
  but had no role-bound FrameInstance contract or participant-threaded motif
  matcher.
- After metrics: the census compiled 27 frame candidates with 54 exact role
  bindings (27 source + 27 target), zero unbound arguments, and zero accepted
  writes. Seven strict motif windows yielded 4 confirmed candidates, 1
  provisional candidate, and 2 rejected observations; transition evidence was
  11 directional, 3 shared-participant, and 5 disconnected. The M12 missing-
  own-condition negative rejected once, and missing/gapped/reordered M03
  negatives matched zero. Current predicate-lane coverage is honestly 8/16
  superframes and 4/12 motifs; the generic matcher interprets 12/12.
- Tests by tier: focused 36 passed
  (`/tmp/t92_canonical_snapshot_focused.log`); senior-
  required whole-tree import preflight collected 222 tests and passed
  (`/tmp/t92_tree_preflight_retry.log`); the exact whole-tree suite produced
  213 passed / 10 optional skips (`/tmp/t92_tree_integrated_final.log`); the
  separate
  checked-in GLiREL-runtime C2 suite passed 4/4 (`/tmp/t92_c2_host.log`).
  Static/secret/side-effect checks passed (`/tmp/t92_static.log`).
- Cross-corpus test: not applicable to this schema/recipe contract task. It
  changes no retrieval behavior or corpus state; T9.3 owns claim-grounded
  parent semantics after an explicit paid-pass gate.
- Failure/rollback test: missing, gapped, reordered, unknown-ID, malformed
  policy, role-vocabulary drift, missing thread closure, and M12 qualifier
  cases all fail closed. Rollback removes only candidate contracts, recipes,
  and tests; persisted state does not exist.
- Deployment image/health: no deploy or service reload. The final suite ran
  from an isolated whole-tree copy preserving production repository geometry.
- Remaining risks: frame-specific semantic role inventories remain deferred
  to owner ontology; v1 binds relation direction only. ClaimRecordV1 currently
  admits only subject/object, making unbound count zero definitional. Current
  T9.1 reachability cannot realize 8 of 12 motifs. Any role-vocabulary or
  tolerance expansion requires a new schema/recipe version.
- Artifact: `docs/baselines/T9_2_FRAME_MOTIF_2026-07-14.json`; two fresh
  whole-tree reports and the frozen artifact were byte-identical at SHA-256
  `0653d81af9650d53fab293939e33644a42fb802e9cf3b84d83a293b432ac46c3`.
- Checklist boxes closed: T9.2 in `CODEX_MISSION.md`; no retrieval, provider,
  or semantic-promotion checklist box is claimed.

### 2026-07-14 - T9.3 digest-provider preflight and LongCat Tier3 route verification

- Commit: this commit on `claude-continuation-20260713`; the paid parent pass
  remains a separate sealed action pending a named senior GO.
- Owner: Codex sole executor under `CODEX_MISSION.md`; Claude authorized one
  Flash probe, one 10-packet LongCat canary, zero-call diagnosis, and one
  versioned parameter-only re-canary with a preregistered 9/10 minimum.
- Corpus/data scope: exactly 10 evenly sampled valid parents from the one-
  document `UGO_CORPUS` (`bcf80054-7611-47d0-ae16-fa7fed259b13`, 203 eligible
  parents). Mark remained virgin. Packets use accepted parent text and
  `polymath.extract.v1` entities, never Ghost A summary text.
- Code changes: added redacted LiteLLM usage/cost telemetry, single-route
  capability probing, serial ceiling enforcement, honest per-packet success/
  DLQ receipts, a versioned LongCat price card, and an exact versioned route-
  parameter card. The only provider remediation was `max_tokens` 4096→8192.
  Prompt `parent-digest.v5`, repair `parent-digest-repair.v2`, SemanticDigestV1,
  semantic validator, Tier3 forced tool, temperature 0, and thinking disabled
  were unchanged.
- Durable migration/backfill: none. Accepted digests live only in
  `semantic_digest_cache` with `canonical_write=false`; failures live only in
  `semantic_digest_dead_letters`. Mongo `semantic_artifacts`, Qdrant points,
  and Neo4j counts were exactly unchanged before/after both canaries.
- Before metrics: Flash again rejected native `json_schema` in one tiny call
  (HTTP 400, cost $0). The first LongCat run at 4,096 output tokens accepted
  5/10, terminal-DLQ'd 5/10, made 19 calls, used 212,353 tokens, and cost
  $0.31691455 by the approved published-list provider-card fallback.
- After metrics: zero-call diagnosis found all 5/5 DLQs exactly at their two-
  attempt 8,192-token aggregate cap and 0/5 accepted packets at cap; packet
  byte size did not separate outcomes. With the frozen 8,192 route cap, the
  single re-canary accepted 10/10 with zero terminal DLQ: 6 first-attempt and
  4 repaired, including the injected semantic mismatch. All 10 provenance
  rows are complete and semantic replay is clean. Fourteen calls used 169,544
  tokens and cost $0.26999080; cumulative preflight cost was $0.58690535,
  below the $2 envelope.
- Tests by tier: telemetry/pricing focused gates passed 58 tests
  (`/tmp/t93_telemetry_pricing_focused.log`); parameter-card and registry gate
  passed 60 (`/tmp/t93_recanary_parameter_focused.log`); read-only live
  preflight passed (`/tmp/t93_live_preflight.log`); Flash probe true `EXIT=0`
  (`/tmp/t93_flash_probe.log`); first LongCat acceptance correctly failed
  `EXIT=1` (`/tmp/t93_longcat_ugo_canary.log`); diagnosis passed `EXIT=0`
  (`/tmp/t93_longcat_diagnosis.stderr`); re-canary passed true `EXIT=0`
  (`/tmp/t93_longcat_ugo_recanary.log`).
- Cross-corpus test: intentionally not applicable to the senior-bounded UGO-
  only preflight. Mark is reserved for the one paid pass and was not sampled.
- Failure/rollback test: the 5/10 initial result was not weakened or retried
  silently. Five exact DLQ IDs and five accepted cache IDs were frozen. The
  diagnosis changed no provider state. Route arguments now fail closed on any
  drift from the versioned 8,192 card. Rollback removes telemetry/cards and
  noncanonical canary rows only; canonical stores require no rollback.
- Deployment image/health: no production deploy or service reload. Live calls
  ran from an isolated current-source copy of the healthy deployed backend
  image through the existing healthy LiteLLM proxy, using encrypted Mongo
  credentials only.
- Remaining risks: the provider route is verified only for Tier3 at the frozen
  8,192 parameter version; native Tier1 remains rejected. LiteLLM lacks a
  numeric LongCat price mapping, so cost uses usage × the versioned published
  list price (conservative uncached input), explicitly named in every receipt.
  The paid pass remains sealed until the senior names provider, packet count,
  and cost ceiling.
- Artifacts: `docs/baselines/T9_3_PROVIDER_PREFLIGHT_2026-07-14.json` preserves
  the failed initial canary; `docs/baselines/T9_3_PROVIDER_RECANARY_2026-07-14.json`
  preserves the green route evidence; runtime permission is recorded in
  `backend/registries/structured_output_capabilities.v1.json`.
- Checklist boxes closed: provider preflight only. T9.3's paid pass,
  projection, and activation work remain open and separately gated.

### 2026-07-14 - T9.3 mark Phase-1C release, legacy fence, and owner quality sample

- Commit: this commit on `claude-continuation-20260713`; Phase 2 remains
  explicitly owner-sealed.
- Owner: Codex sole executor under `CODEX_MISSION.md`; Claude froze the v6/v3
  prompt remediation, phase-selection ledger, bounded-exposure accounting,
  five-parent tail, census_scope.v2 correction, and backup-first fence. The
  owner required an actual digest sample before authorizing Phase 2.
- Corpus/data scope: `markbuildsbrands_transcripts`, corpus
  `5a20bc21-95df-42c2-80c8-f927b4e83904`, 103 documents / 989 eligible
  parents. Phase 1 bought 12 v5/v2 packets, Phase 1B bought 10 fresh v6/v3
  packets, and Phase 1C bought 50 fresh v6/v3 packets. Accepted cross-prompt
  cache rows were never repurchased.
- Code changes: added the durable paid-pass runner with deterministic fresh
  selection, checkpoint/release prerequisites, exactly-once claims, certified
  cross-prompt cache skip, cost/size/acceptance stops, and an authorization-
  scoped tail identity. Added honest transport exposure accounting: unknown
  actual cost remains null/incomplete while a separate versioned `$0.06`
  bound participates in ceiling arithmetic. Added hashed
  `canonical_store_census.scope.v2`: only explicit Polymath Qdrant collection
  rules have verdict authority; co-tenant counts/deltas remain visible.
- Durable migration/backfill: 66 accepted noncanonical cache rows and 6 DLQs
  exist across the 72 purchased target packets. Thirty-eight unattempted
  Phase-1 materializations remain checkpoint-cancelled. Before Phase 2, 939
  inert legacy v5/v2 queued rows were exported to sorted canonical JSONL and
  then superseded with reason `superseded_by_phase2_v6_2026-07-14`; active
  target rows are zero. No semantic artifact, Qdrant point, Neo4j node, or
  canonical projection was written.
- Before metrics: Phase 1 accepted 8/12 and DLQ'd 4/12. Senior-approved v6/v3
  Phase 1B accepted 10/10. Phase 1C first stopped at 37 accepted / 2 DLQ / 11
  untouched because ordinal 60 had a transport ReadTimeout, no usage telemetry,
  and null actual cost.
- After metrics: the approved continuation accepted 11/11, so Phase 1C is
  48/50 = 96% with two final DLQs and one bounded transport exposure. Phase-1C
  known actual is `$1.14107950` plus `$0.06` bound; cumulative mark known
  actual is `$1.68454455`, ceiling basis `$1.74454455` under `$49.45`. All 50
  packets are at/below 21,515 bytes, max 16,918. Fifty green release and
  canonical checkpoint markers persist. Frozen Phase-2 remainder is 917.
- Tests by tier: bounded accounting focused 19/19 and adjacent 159/159; final
  census_scope.v2 focused 41/41, immediate adjacent 91/91, and broad adjacent
  193 passed / 7 optional skips. The zero-provider v2 postflight returned true
  `EXIT=0` with empty call receipts. Backup and fence gates both returned true
  `EXIT=0`; owner-report structure/secret/diff validation returned true
  `EXIT=0`.
- Cross-corpus test: census_scope.v2 observes all live Qdrant collections,
  partitions exact Polymath-owned collections from ambient co-tenants, and
  still fails on any protected Mongo/Qdrant/Neo4j drift. The triggering global-
  v1 receipt detected one `hermes_memories` point; access logs and payload
  shape attributed it to the host-side Hermes/mem0 co-tenant. The RED receipt
  remains immutable. The new v2 postflight reported ambient counts and found
  protected drift zero.
- Failure/rollback test: no failed gate was relabeled. The ReadTimeout was not
  retried in-phase or assigned synthetic cost. The 939-row fence is reversible
  from its pre-mutation JSONL, SHA-256
  `eceff199ac6ed56933d21793860005cef743b81484c07dff20b3321a7b26e72d`.
  Phase 2 cannot materialize until the release marker exists and now remains
  additionally blocked by the owner decision.
- Deployment image/health: no rebuild or restart occurred during paid work.
  Tests and operations ran from the exact isolated source overlay on the
  healthy deployed backend image. Host/container hashes matched before the
  postflight. Credentials came only from encrypted Mongo settings.
- Remaining risks: the owner sample proves that acceptance is contract/grounding
  reliability, not semantic richness. Eight of 66 accepted digests are bare
  headings (two still propose a latent concept); evidence is one whole-parent
  interim claim rather than an atomic compiled claim; domain proposals cover
  only 13/66 accepted parents. The owner report recommends keeping Phase 2 on
  hold until choosing candidate-only bulk execution or a deterministic noise,
  claim-granularity, and domain-coverage policy. The five-parent tail remains
  sealed until Phase 2 and corpus-wide acceptance at least 95%.
- Artifacts: immutable RED continuation receipt
  `docs/baselines/T9_3_MARK_PHASE1C_CONTINUATION_RED_2026-07-14.json`; green v2
  release receipt
  `docs/baselines/T9_3_MARK_PHASE1C_CENSUS_V2_RELEASE_2026-07-14.json`; owner
  report `docs/T9_3_MARK_DIGEST_SAMPLE_FOR_OWNER_2026-07-14.md`; backup
  `docs/baselines/t93_backups/T9_3_MARK_LEGACY_SEMANTIC_DIGEST_JOBS_QUEUED_2026-07-14.jsonl`
  plus its SHA-256 sidecar.
- Checklist boxes closed: Phase-1C release and the pre-Phase-2 legacy fence.
  T9.3 remains open: Phase 2, corpus-wide completion, and bounded tail are
  owner/sequencing gated; no projection or retrieval activation is claimed.

### 2026-07-14 - T9.4 provider-neutral parity and burst-safety contracts

- Commit: this commit on `claude-continuation-20260713`.
- Owner: Codex sole executor under `CODEX_MISSION.md`; Claude approved the
  deterministic T9.4 slice with four boundaries: measurement never
  adjudication, executor-proposed/owner-ratifiable authority, CP1-D2a as the
  one completeness truth, and first-class per-lane failure/fallback accounting.
- Corpus/data scope: synthetic contract fixtures only. No UGO, mark, ecommerce,
  or v2 corpus row was read; the PoC-pair 5,000-chunk gate did not run.
- Code changes: added strict `candidate_extraction_artifact.v1`, additive pure
  adapters for cloud/private-local/legacy-local/RunPod `ExtractionResult`
  shapes, a same-chunk measurement-only parity report, and pure disposition/
  durable-job barrier/burst-metrics contracts. Exported the exact existing
  same-contract terminal-artifact predicate used by extraction-job
  reconciliation so retry receipts reuse one decision.
- Durable migration/backfill: none. No queue row, extraction, summary, vector,
  graph artifact, manifest, readiness row, or projection was persisted.
- Before metrics: engines shared downstream dataclasses but had no one strict
  candidate/provenance schema, no symmetric like-for-like delta report, and no
  receipt contract joining the disposition matrix to CP1-D2a durable-job
  truth and per-lane burst accounting.
- After metrics: shared schema hash is
  `sha256:370661b1059bb5c3e7027033d0dba91f399686eda5895bbe780dc39bb620d229`.
  Parity reports engine/runtime/model/source-wire identities plus entity,
  relation, exact-evidence, ontology, graph-eligibility, failure, and fallback
  measures without a verdict/winner field. Burst manifests dispatch only when
  all valid chunks are accounted terminal-or-runnable by exact durable job
  identity; metrics expose chunks/sec, worker/billed seconds, cost per 1k, and
  per-lane failures/fallbacks.
- Tests by tier: focused 18/18 passed
  (`/tmp/t94_focused_final.log`); adjacent extraction/RunPod/durable-job/
  readiness/graph/local-extraction boundary passed 255/255 with 8 existing
  warnings (`/tmp/t94_adjacent_post_scope.log`); six new files are Black-clean,
  all seven changed Python files compile, and diff check is clean
  (`/tmp/t94_static.log`).
- Cross-corpus test: intentionally not run. The contract supports corpus-bound
  manifests but the owner-designated mark/ecommerce 5,000 gate and actual
  engine comparison remain blocked by the senior's production boundary.
- Failure/rollback test: duplicate engine/chunk rows, source-text mismatch,
  incomplete metadata/chunking, active ingest, owner-pending/projection-only
  dispositions, unfinished pipeline jobs, chunk/contract identity drift,
  ungrounded object kinds/cues, and incomplete burst accounting all fail
  closed. Rollback removes code/tests only; no durable state exists.
- Deployment image/health: no rebuild, restart, endpoint deploy, or production
  readiness stamp. Tests ran from an isolated overlay of the healthy backend
  image with current registry fixtures.
- Remaining risks: the full provider-independent lexicon projector remains
  open; current RunPod responses still need a pinned image/pipeline version
  surface; live readiness wiring and the 100/500/5,000 measured gates remain
  open; no engine has been adjudicated and RunPod remains non-production.
- Checklist boxes closed: the first six P2.6 shared-contract/provenance/
  grounding-rule bullets only. P2.6 projector parity, all corpus-scale P2.7
  acceptance, and live P2.7b orchestration remain open.

### 2026-07-15 - T9.3 Lane B B1/B2/B3 zero-spend closure

- Commit: this commit on `claude-continuation-20260713`; B4 and Phase 2 remain
  separately gated and no provider execution is included.
- Owner: owner selected Lane B fix-then-buy. The senior approved the generic
  B1 threshold, pinned-host/canonical-image B2 compiler boundary, explicit
  packet-contract bump, historical ledger, bounded packet-v2 design, two
  no-claim-child exclusions, three-negative capacity residual, and B3 policy;
  senior certification is recorded at 2026-07-15T01:12:21Z in
  `COORDINATION.md`.
- Corpus/data scope: `markbuildsbrands_transcripts`, corpus
  `5a20bc21-95df-42c2-80c8-f927b4e83904`, 99 documents. B1 classifies 989
  structurally valid nonempty parents as 99 heading-only, 95 below the
  256-substantive-byte threshold, and 795 eligible. B2 closes over 3,493
  unique child chunks.
- Code changes: added frozen `semantic_parent_eligibility.v2`, strict result
  model, mark audit, and 8-row known-heading fixture; immutable
  `ClaimCompilationMaterializationRowV1` export/import/readback boundary;
  bounded `semantic_parent_packet.atomic_claims.v2` with exact five-field
  provider claims, one-per-child closure, 20,000-byte typed→negative→nuanced→
  ordinary child-round-robin skip-and-continue selection, emitted-only
  validator scope, full source/emitted/excluded manifests, and byte-decision
  ledgers. B3 records that T9.1 owns domain coverage and model proposals are
  auxiliary, lawfully sparse candidates.
- Durable migration/backfill: additive collection
  `semantic_digest_claim_compilations` contains exactly 3,493 immutable
  `canonical_write=false` candidates and zero unsafe/missing flags. Canonical
  semantic artifacts, Qdrant, Neo4j, retrieval, projections, summaries, and
  historical accepted digests were not changed.
- Before metrics: 989 structural parents included 194 deterministic noise
  rows. The first lossless atomic packet projection measured p50 301,642 / p95
  360,251 / max 549,701 bytes and was frozen unused. Historical ledger is 66
  accepted, 6 DLQ, 939 superseded, and 38 checkpoint-cancelled; 52 accepted +
  4 DLQ overlap B1 eligibility.
- After metrics: compiler export contains 84,586 claims (349 typed / 84,237
  untyped), 30,880 evidence sentences, and 2 claim links; two independent
  exports are byte-identical at raw JSONL SHA-256
  `d29215a412e68bace7395291a3387d44a02c5b1d1d88afeae5a7a6e13b6a0a52`.
  Production packet-v2 resolves 793 ready + 2 permanently ledgered
  `source_child_without_atomic_claim` parents. Ready claims are 84,247 source /
  20,960 emitted; typed retention is 347/347 and negative is 5,873/5,876. The
  three excluded negatives all belong to one parent, were attempted twice,
  exceeded the cap by 109–115 bytes, and remain locally authoritative. Packet
  bytes p0/p50/p95/max are 9,142/19,870/19,996/20,000; complete fresh-process
  receipts are byte-identical with 793 unique packets and set hash
  `sha256:00960dbeb9d1704421a79ea1abd3b71112e316c66143b2cfe507c709c624bf04`.
- Tests by tier: B1 host/canonical 9/9 each; packet-v2 final focused host 44/44
  and canonical 33 passed / 11 expected trained-spaCy skips. Full production
  census and replay are `EXIT=0`; complete JSON comparison is `EXIT=0`.
  Formatting and static gates are green after all disclosed harness/assertion/
  formatting corrections. Receipt pointers:
  `/tmp/b2_v2_priority_exception_host.log`,
  `/tmp/b2_v2_priority_exception_canonical.log`,
  `/tmp/b2_packet_v2_census_v3.log`,
  `/tmp/b2_packet_v2_census_replay.log`, and
  `/tmp/b2_packet_v2_replay_compare.log`.
- Cross-corpus test: intentionally not applicable. This mark-only lane changes
  no retrieval or shared live projection. The later owner-authorized fresh
  ecommerce E2E remains sequenced behind modular completion and RunPod parity.
- Failure/rollback test: missing source closure, source drift, missing atomic
  claims, duplicate rows, noncanonical-flag drift, packet oversize, schema
  extras, invalid timestamps, packet nondeterminism, and exclusion-accounting
  drift fail closed. Rollback removes candidate rows and additive code only;
  no canonical state requires rollback.
- Deployment image/health: no deploy, rebuild, restart, endpoint change, or
  paid batch. Canonical gates used all five active compose overlays with a
  read-only `/app` source bind; pinned host performed the certified spaCy
  compile. Credentials were neither printed nor moved.
- Remaining risks: provider proposal space is deliberately bounded to emitted
  claims; excluded claims remain local. Two parents require a separately
  approved compiler-policy decision if revisited. B4 must freeze size strata,
  include top-decile representation, use current price cards, and perform a
  visible per-packet summary-faithfulness review. Phase 2 remains blocked on
  senior verification plus owner/standing gates.
- Artifacts: `docs/T9_3_LANE_B_B1_B2_DESIGN_2026-07-14.md`,
  `docs/T9_3_ATOMIC_PACKET_V2_BOUNDED_DESIGN_2026-07-15.md`, and
  `docs/T9_3_B3_DETERMINISTIC_DOMAIN_AUTHORITY_2026-07-15.md`.
- Checklist boxes closed: B1 eligibility, B2 atomic compilation/bounded packet
  contract, and B3 domain-authority policy only. T9.3 B4, Phase 2,
  corpus-wide acceptance, bounded tail, projection, and activation stay open.

### 2026-07-15 - T9.3 Lane B B4 zero-provider preflight and paid-run seal

- Commit: this commit on `claude-continuation-20260713`; it publishes tested
  execution code but contains no provider output or paid-call receipt.
- Owner: the owner selected Lane B. The senior restated exact B4 GO at
  2026-07-15T01:31:30Z for ten packets, ceiling `$0.42995425`, >=9/10
  acceptance, all-ten summary review, and noncanonical storage only.
- Corpus/data scope: mark's 793 packet-ready parents minus 56 historically
  purchased-ready = 737 fresh; two no-claim-child parents remain permanent
  nonready exclusions and were not purchased.
- Code changes: added a versioned five-band selector and credential-blind
  preflight; exposed its immutable prepared selection to a separate paid
  runner. The runner rederives every full packet/selection/prompt/repair/schema
  hash, requires the exact decimal authority, uses serial max-attempts=1 jobs,
  selection-scoped cost accounting, a two-ReadTimeout pause, and protected
  census_scope.v2 before/after comparison.
- Durable migration/backfill: none. The preflight and negative execution-seal
  test made no job/cache/canonical write. Post-seal census found zero B4
  selection rows, zero B4 phase rows, and zero active B4 lane leases.
- Before metrics: 737 fresh packets. Rank-band populations are 185/184/184/
  111/73 at q00–25/q25–50/q50–75/q75–90/top-decile.
- After metrics: exactly two packets per band, ten unique documents, total
  198,938 packet bytes, population hash
  `sha256:00960dbeb9d1704421a79ea1abd3b71112e316c66143b2cfe507c709c624bf04`,
  selection hash
  `sha256:55ab1e846c40ef2e3a233a01f3333758b9660451b3237241f1976e271d9f203f`,
  exact selected ceiling `$0.42995425`, and 727 fresh remaining after B4.
- Tests by tier: combined B1–B4 host regression 34/34; runner/preflight host
  16/16 plus compile; canonical image 16/16 plus Black; diff check green.
  Receipt pointers: `/tmp/b123_b4_combined_host_v3.log`,
  `/tmp/b4_runner_host_gate.log`, `/tmp/b4_runner_compile_gate.log`,
  `/tmp/b4_runner_canonical_gate_v2.log`,
  `/tmp/b4_runner_canonical_black_gate_v2.log`.
- Cross-corpus test: not applicable to this frozen mark-only paid canary.
- Failure/rollback test: invalid authorization completed the full live
  read-only reconstruction then failed before credentials, job writes, or
  calls (`/tmp/b4_atomic_seal_negative.log`, expected-negative wrapper green;
  post-state `/tmp/b4_atomic_seal_state_v2.log`). Exact identity, count,
  provider-card, cost, active-job, and canonical-drift checks fail closed.
- Deployment image/health: no rebuild or endpoint deploy. Pure gates and live
  seal used the healthy canonical backend image with current source mounted;
  credentials were not printed or moved.
- Remaining risks: the paid command and per-digest faithfulness review remain
  next. Phase 2 stays sealed through final B4 review and the owner window.
- Checklist boxes closed: B4 zero-provider selection/cost preflight and
  execution seal only; paid acceptance, Phase 2, tail, projection, and
  activation remain open.

### 2026-07-15 - T9.3 Lane B B4 failed canary, ceiling control, and v3 proposal

- Commit: this commit on `claude-continuation-20260713`.
- Owner: B4 used the exact senior GO at 2026-07-15T01:31:30Z. The senior
  classified the canary failed, rejected claims-only v2 with evidence, mandated
  the universal reservation/two-attempt authority control, and invited a
  sentence-anchored v3 note. No new paid GO exists.
- Corpus/data scope: frozen mark ten-row selection from 793 packet-ready
  parents. Purchased artifacts remain noncanonical; two no-claim-child parents
  remain excluded. No corpus, projection, or retrieval state was changed.
- Code changes: added shared ceiling-rounded two-attempt cost authority and
  per-claim reservation. Both serial and concurrent paid paths reserve before
  claim; concurrent claims are limited to the cumulatively affordable prefix.
  B2/B4 authority producers use the same formula. Exact-boundary, one-quantum-
  inside, fail-closed inputs, and runner-does-not-claim regressions are present.
- Durable migration/backfill: none. The canary wrote only its durable
  noncanonical job/cache/DLQ evidence. Control and design measurements made no
  database writes.
- Before metrics: B4 authority `$0.42995425`, acceptance bar >=9/10, ten queued
  packets, protected stores Mongo semantic 0 / Qdrant 1,364,159 / Neo4j
  1,361,818 nodes and 3,712,432 relationships.
- After metrics: 4 accepted, 5 DLQ, 1 queued/unclaimed, 15 calls, known cost
  `$0.45429295`; exact ceiling overage `$0.02433870`. Protected and ambient
  canonical census is exactly unchanged. Within-authority acceptance was 3/8;
  final durable acceptance 4/9; strict faithfulness 2/4.
- Tests by tier: host and isolated canonical focused/adjacent suites 57/57;
  canonical Black, compile, and diff checks all true `EXIT=0`. Exact pointers
  are `/tmp/paid_reservation_authority_host_gate.log`,
  `/tmp/paid_reservation_authority_canonical_gate.log`,
  `/tmp/paid_reservation_authority_black_gate.log`,
  `/tmp/paid_reservation_authority_compile_gate.log`, and
  `/tmp/paid_reservation_authority_diff_gate.log`.
- Cross-corpus test: not applicable; no retrieval behavior or shared live
  projection changed. The reservation helper is corpus-agnostic and is wired
  into both paid mark execution paths.
- Failure/rollback test: all five DLQs are structural EOF failures. Read-only
  raw-body classification proves all ten attempts are zero-byte
  `empty_tool_arguments`; semantic/transport/cap/unpriced counts are zero.
  The boundary regression proves no durable claim when basis is within one
  reserved envelope of authority. Additive code can be reverted; purchased
  evidence is retained and never rewritten.
- Deployment image/health: no rebuild, deploy, restart, or endpoint mutation.
  Isolated tests used the canonical image and read-only source mount. No key or
  raw provider text was printed, moved, or committed.
- Remaining risks: claims-only v2 is rejected; Phase 2 and owner sample window
  are sealed. The measured v3 note finds only 80.944158% of evidence sentences
  have atomic mappings and counter-proposes ordered units with optional claim
  IDs. Senior/owner must approve the contract and 26,000-byte cap; any future
  canary requires pure tests, live zero-provider preflight, corrected exact
  selected authority, and a fresh GO.
- Artifacts: `docs/T9_3_B4_FAILURE_RECEIPT_2026-07-15.md` and
  `docs/T9_3_SENTENCE_ANCHORED_PACKET_V3_PROPOSAL_2026-07-15.md`.
- Checklist boxes closed: B4 execution and diagnosis are closed as **failed**;
  the universal ceiling control is implemented/tested. T9.3 paid completion,
  Phase 2, tail, canonical projection, and activation remain open.

### 2026-07-15 - T9.3 sentence-hybrid v3 contract and zero-provider preflight

- Commit: this commit on `claude-continuation-20260713`; no provider output or
  paid execution is included.
- Owner: Lane B remains the owner-selected route. The senior approved ordered-
  unit v3 with a 26KB cap and four riders at 2026-07-15T03:02:04Z, then issued
  a separate exact canary GO only after this preflight at 03:39:13Z.
- Corpus/data scope: mark's 795 eligible parents; 793 packet-ready and the same
  two `source_child_without_atomic_claim` exclusions. Candidate compilations
  were read; no corpus, canonical semantic, graph, or vector row was changed.
- Code changes: strict citable/context-only sentence unit models; mapped/
  unmapped per-packet disclosure; deterministic source-order builder; local
  sentence→atomic expansion with stale/cross-closure failure; version-neutral
  schema-contract hash; frozen five-band/long-packet selector; credential-blind
  preflight with corrected reservations and cumulative-umbrella arithmetic.
- Durable migration/backfill: none. The live preflight made zero provider
  calls, credential plaintext reads, database writes, canonical writes, and
  projection writes.
- Before metrics: measured proposal was 793+2, 30,694 evidence sentences with
  80.944158% mapping, max 25,613 bytes, max-any-ten `$0.83486975`. One first
  strict serialization attempt exposed 28,041 bytes because absent optional
  entity fields were emitted as null/defaults; it failed before any call/write.
- After metrics: all 30,694 sentences retained, 24,845 mapped / 5,849 context-
  only, dropped=0. Packet bytes min/p50/p90/p99/max are 3,421/13,917/15,206/
  16,091/25,601; 3 are >20KB and 0 >26KB. Packet/schema hashes are `89ace7ed...`
  / `5c600d30...`. Fresh population is 728 after 81 purchased parents; the
  unique-document selection hash is `6aed7b1a...`, includes one >20KB packet,
  and has exact authority `$0.78260930`. Max-any-ten is `$0.83466680`.
- Tests by tier: host 28/28; backend and ingest-worker isolated canonical
  overlays each 27 passed + one expected trained-spaCy skip. Live preflight,
  Black, both-container compile, and diff checks all true `EXIT=0`. Receipt:
  `docs/T9_3_SENTENCE_HYBRID_V3_PREFLIGHT_RECEIPT_2026-07-15.md`.
- Cross-corpus test: not applicable; this is a mark-only noncanonical provider-
  packet change and changes no retrieval or shared projection.
- Failure/rollback test: context-only citations, stale revisions, cross-parent/
  child mappings, empty/unstable maps, count tampering, over-26KB packets, long-
  stratum loss, document duplication, and reservation-boundary breach fail
  closed. Additive code can be reverted; no canonical rollback is required.
- Deployment image/health: no rebuild, restart, endpoint mutation, or paid
  batch. Both running image roles were tested using isolated current-source
  overlays; protected census_scope.v2 was exactly unchanged.
- Remaining risks: the authorized ten-packet canary is not executed in this
  commit. Its runner must seal exact hashes and `$0.78260930`, then all accepted
  outputs require strict faithfulness review. Phase 2 remains separately gated.
- Checklist boxes closed: ordered-unit v3 pure contract and live zero-provider
  preflight only. Paid canary, owner sample/window, Phase 2, tail, projection,
  and activation remain open.

### 2026-07-15 - T9.3 sentence-hybrid v3 paid-runner seal

- Commit: this commit on `claude-continuation-20260713`; the runner is committed
  before any use of the senior's paid GO.
- Owner: Lane B is owner-selected; the exact selected-ten canary GO is the
  senior entry at `COORDINATION.md:2026-07-15T03:39:13Z`.
- Corpus/data scope: read-only rederivation of the frozen mark population and
  selection. No provider output or canonical store is in this change.
- Code changes: v3-only serial runner; exact GO/population/provider/authority
  assertions; shared two-attempt reservation guard; before/after cumulative
  umbrella checks; phase-parametric reuse of the previously sealed B4 queue
  helpers; safe failure receipt.
- Durable migration/backfill: none. The live invalid-GO seal stopped before
  job materialization and left zero v3 phase rows, calls, selections, or lane
  leases.
- Before metrics: approved packet/schema/selection hashes `89ace7ed...` /
  `5c600d30...` / `6aed7b1a...`; exact ten-packet authority `$0.78260930`.
- After metrics: no paid outcome yet. The wrong-authorization replay reached
  the intended `PaidPassError` boundary and its independent state census was
  all zero.
- Tests by tier: host/backend/worker 43/43 each; Black, host/backend/worker
  compile, and diff checks all true `EXIT=0`. Permanent pointer:
  `docs/T9_3_SENTENCE_HYBRID_V3_RUNNER_SEAL_RECEIPT_2026-07-15.md`.
- Cross-corpus test: not applicable; noncanonical mark-only paid runner.
- Failure/rollback test: every GO identity and authority argument, exact and
  one-quantum-short umbrella boundaries, bad authorization before paid-path
  settings/credential access, and no-claim-without-reservation are covered.
- Deployment image/health: no rebuild, restart, deployment, or endpoint
  mutation. Current source was tested in isolated backend and worker overlays.
- Remaining risks: the canary result and all-output faithfulness review remain
  unobserved. Phase 2 remains gated by a green canary, three-digest owner
  sample, and the 60-minute owner window.
- Checklist boxes closed: runner seal only; no T9.3 paid completion box closes.

### 2026-07-15 - T9.3 sentence-hybrid v3 canary execution and diagnosis

- Commit: terminal receipt commit on `claude-continuation-20260713`; runner
  code was already sealed and pushed as `63c6f3d` before execution.
- Owner: exact senior GO at `COORDINATION.md:2026-07-15T03:39:13Z`; Phase 2
  gate was >=9/10 accepted plus strict all-accepted faithfulness.
- Corpus/data scope: ten frozen mark parents from ten documents across all
  five packet-size bands, including one packet >20KB. Noncanonical only.
- Code changes: no execution-time change. Postflight diagnosis reconstructed
  the historical packets read-only from durable parent IDs and frozen source
  revisions; it did not rerun the advancing fresh selector as historical
  replay.
- Durable migration/backfill: ten noncanonical jobs, two accepted cache rows,
  and eight DLQ rows are retained as purchased evidence. No projection or
  canonical write occurred.
- Before metrics: 10 selected, >=9 required; `$0.78260930` hard authority;
  cumulative umbrella basis `$2.19883750` of `$49.45`.
- After metrics: **2 accepted / 8 structural DLQ**, 19 calls, 0 ReadTimeouts,
  `$0.55765220` complete known cost, cumulative basis `$2.75648970`. Protected
  and ambient census are exactly unchanged.
- Tests by tier: execution true runner `EXIT=1` under the preregistered red
  gate; read-only DLQ classifier and exact-context postflight both true
  `EXIT=0`. Permanent receipt:
  `docs/T9_3_SENTENCE_HYBRID_V3_CANARY_FAILURE_RECEIPT_2026-07-15.md`.
- Cross-corpus test: not applicable; mark-only noncanonical provider canary.
- Failure/rollback test: 14/16 failed bodies are zero-byte empty tool arguments;
  two are structurally valid JSON with invalid claim citations. Six jobs are
  empty on both attempts. Authority, cumulative umbrella, and canonical-store
  invariance all held; no rollback is required.
- Deployment image/health: no rebuild, restart, endpoint mutation, or other
  provider lane while the paid batch ran.
- Remaining risks: LongCat Tier 3 is structurally unreliable for both claims-
  only and ordered-unit structured packets. Accepted-output faithfulness is
  2/2, but cannot rescue 2/10 structural acceptance. Cross-shape acceptance is
  interim prose 10/10, claims-only 3/8 within authority, ordered-unit 2/10.
- Checklist boxes closed: v3 canary execution/diagnosis closes **failed**.
  Phase 2, tail, owner sample/window, projection, and activation remain open or
  parked; no T9.3 paid-completion box closes.

### 2026-07-15 - T9.4 current-field lexicon-projector parity

- Commit: this commit on `claude-continuation-20260713`.
- Owner: non-paid Track-A/E1 continuation under the senior's standing mission
  directive; no live/paid production authority is consumed.
- Corpus/data scope: pure synthetic same-document candidate artifacts only.
  No UGO, mark, ecommerce, or v2 row was read or written.
- Code changes: added one candidate-artifact compatibility adapter and an
  optional explicit-artifact input to the existing document projector. The
  legacy `ghost_b_extractions` path is unchanged when the new input is absent;
  engine/runtime/model provenance never branches projector behavior.
- Durable migration/backfill: none. No artifact, lexicon source, lexicon row,
  vector, graph edge, readiness row, job, or manifest was persisted.
- Before metrics: strict candidates and the existing lexicon projector had no
  direct pure seam proving all four candidate engine labels used the same
  query-translation projection.
- After metrics: cloud, local, legacy-local/RTX, and RunPod artifacts carrying
  the same semantic payload produce field-identical document sources and
  materialized current fields after excluding only nondeterministic
  `updated_at`. Covered outputs include co-occurrence, parent-derived
  contextual usage, source-backed factual relations, retrieval gloss, and
  representation eligibility.
- Tests by tier: host focused 2/2; final backend canonical 30/30 and worker
  canonical 30/30, each with 7 existing Pydantic namespace warnings; scoped
  Black, host/backend/worker compile, engine-blind audit, and diff checks all
  true `EXIT=0`. Permanent receipt:
  `docs/T9_4_CURRENT_FIELD_LEXICON_PROJECTOR_RECEIPT_2026-07-15.md`.
- Cross-corpus test: intentionally not run. This proves projector
  engine-blindness for equal strict inputs, not empirical equality of outputs
  produced by different engines on the PoC pair.
- Failure/rollback test: wrong document ownership, duplicate or absent chunks,
  stale source text, shared-contract drift, and failed artifacts reject before
  projection. Rollback removes additive code/tests only; durable rollback is
  unnecessary.
- Deployment image/health: no rebuild, restart, deploy, endpoint call, or
  production stamp. Exact working-tree bytes passed isolated overlays of both
  healthy canonical containers.
- Remaining risks: the host Pydantic 2.13.4 venv derives a different generated
  JSON-schema hash than deployed Pydantic 2.5.0, so canonical runtime pinning
  remains material. Actual engine-output comparison, future P2.1/P2.2 fields,
  pinned endpoint, 100/500/5,000 gates, retry safety, readiness wiring, and
  production acceptance remain open.
- Checklist boxes closed: P2.6 shared-projector implementation for all fields
  that exist now. P2.7 live/corpus-scale parity and every production gate stay
  open.

### 2026-07-15 - T9.3 B1 interim-prose Phase-2 launch seal

- Commit: this commit on `claude-continuation-20260713`; paid execution follows
  only from the published exact bytes.
- Owner: owner selected the proven plain-prose digest contract; senior issued
  the exact execution order at `COORDINATION.md#2026-07-15T05:24:45Z`.
- Corpus/data scope: mark only; 795 B1-eligible parents; exact selection 721 =
  719 fresh plus two rejected-v2 B4-parent re-buys.
- Code changes: dedicated exact-GO Phase-2 runner with durable-ID selection,
  per-claim two-attempt reservation, completion-order rolling stops, exact
  checkpoints, concurrency 3→6 after 100/100 clean, and noncanonical
  supersession history. Certified packet/prompt/gateway behavior is unchanged.
- Durable migration/backfill: none at seal time; zero Phase-2 jobs, calls,
  cache rows, supersessions, projections, or canonical writes.
- Before metrics: prior cumulative basis `$2.7564896999999995`; remaining
  umbrella `$46.69`; old sample-era runner could not express the B1 population
  or durable re-buy identities.
- After metrics: selection hash `ee876928...`; packet-set hash `f867e622...`;
  absolute guard `$49.4464896999999995`; max next reservation `$0.09536318`;
  full two-attempt worst case `$56.48863913`, honestly boundary-guarded.
- Tests by tier: canonical focused 75/75; Black 6/6 unchanged; corrected live
  preflight true `EXIT=0`; invalid-GO true `EXIT=1` followed by exact zero-write
  postcheck true `EXIT=0`. Permanent receipt:
  `docs/T9_3_PROSE_PHASE2_LAUNCH_SEAL_RECEIPT_2026-07-15.md`.
- Cross-corpus test: not applicable; this paid lane is mark-only and
  noncanonical. census_scope.v2 still observes all protected and ambient
  stores.
- Failure/rollback test: three pre-provider failures caught identity, budget-
  interpretation, and hash-namespace defects; all caused zero provider calls
  and zero writes. Runner publication is additive and revertible; purchased
  calls are not.
- Deployment image/health: isolated overlay only; no rebuild, restart, deploy,
  or endpoint mutation before the batch.
- Remaining risks: the `$46.69` umbrella can stop the lane before 721 if actual
  spend plus next worst-case reservation reaches the guard. Main execution,
  50/rolling receipts, completion ledger, owner sample, tail, projection, and
  activation remain open.
- Checklist boxes closed: launch seal only. T9.3 paid completion remains open.

### 2026-07-15 - T9.3 B1 interim-prose Phase-2 rolling-stop closure

- Commit: this receipt commit on `claude-continuation-20260713`.
- Owner: owner authorized the paid interim-prose pass; senior accepted the
  exact preregistered stop and ordered read-only diagnosis under a decision
  criterion fixed before diagnosis.
- Corpus/data scope: mark only; exact 721-parent Phase-2 selection. The first
  attempt settled at 148 terminal = 141 accepted / 7 DLQ, with 573 queued and
  zero running.
- Code changes: none. The published exact-GO runner and frozen provider,
  packet, prompt, repair, schema, budget, and pause contracts were unchanged.
- Durable migration/backfill: 721 durable jobs were materialized; 141 accepted
  noncanonical cache rows and 7 honest DLQs are terminal. One successful B4
  re-buy has append-only supersession history; the other remains queued with
  its source cache unchanged.
- Before metrics: checkpoint 0100 was 99 accepted / 1 structural DLQ, 99%
  acceptance, no timeout, no stop reason, and concurrency 3.
- After metrics: overall attempted acceptance 141/148 = 95.27%, but the
  authoritative completion-order final window is 44/50 = 88%, below the
  frozen 90% floor. Stop reason is
  `rolling_acceptance_below_90_percent`; provider calls are 206. Phase cost is
  `$4.079086600000001` known actual + `$0.12` bounded exposure =
  `$4.199086600000001`; corpus-wide basis is `$6.955576299999998`.
- Tests by tier: immutable execution report runner `EXIT=1` as required for
  RED; safe final-ledger, rolling-window, and re-buy inspections each true
  `EXIT=0`. Execution JSON SHA-256 is
  `a902153e7f9fe02c371e1246719e842e8181c58b78b46eaf4969ecebfeb263ce`.
  Permanent receipt:
  `docs/T9_3_PROSE_PHASE2_ROLLING_STOP_FAILURE_RECEIPT_2026-07-15.md`.
- Cross-corpus test: not applicable; this paid lane is mark-only.
  `canonical_store_census.scope.v2` remains valid, protected stores exactly
  unchanged, and ambient Qdrant change false.
- Failure/rollback test: the frozen rolling gate fired without redefinition;
  no new job was claimed after stop. Purchased results and DLQs are an
  append-only ledger and are not rolled back.
- Deployment image/health: no rebuild, restart, deploy, projection, or
  retrieval activation occurred during the paid attempt.
- Remaining risks: read-only diagnosis must determine document clustering
  versus provider-health drift. At most one unchanged-gate resume is
  conditionally authorized; a second rolling stop parks for owner. Tail,
  three-digest owner sample, corpus-wide completion, projection, and
  activation remain open.
- Checklist boxes closed: this failed execution attempt and boundary-stop
  receipt only. T9.3 paid completion and all downstream boxes remain open.

### 2026-07-15 - T9.3 B1 Phase-2 post-stop read-only diagnosis

- Commit: this diagnosis commit on `claude-continuation-20260713`.
- Owner: senior preregistered the resume/park criterion before diagnosis; no
  new owner scope was inferred.
- Corpus/data scope: read-only inspection of the exact 721-row Phase-2 job set,
  its seven dead letters, document metadata, next 50 queued ordinals, and a
  four-minute LiteLLM status-only log window.
- Code changes: none. Temporary `/tmp` diagnostic helpers read only safe
  metadata and suppress raw log lines/provider output.
- Durable migration/backfill: none; durable state remains 141 accepted / 7
  DLQ / 573 queued / zero running.
- Before metrics: final rolling window 44/50 accepted; causal branch unknown.
- After metrics: `333dd5a6…` accounts for 3/7 total and 3/6 rolling-window
  failures at 30% within-document attempted failure rate. The next 50 queued
  rows span six documents and only one row overlaps any failure document.
  Accepted latency prior-vs-final window is p50 126.537→136.331 seconds and
  p95 273.553→258.548; cost p50 `$0.02460110`→`$0.02688350` and p95
  `$0.04851501`→`$0.04675222`. One timestamp-correlated HTTP 500 exists; no
  repeated 5xx.
- Tests by tier: durable diagnosis true `EXIT=0`; status-only Docker-log parse
  `DOCKER_EXIT=0`. Permanent receipt:
  `docs/T9_3_PROSE_PHASE2_POSTSTOP_DIAGNOSIS_2026-07-15.md`.
- Cross-corpus test: not applicable; no corpus content or canonical stores
  were read or changed beyond safe mark document labels.
- Failure/rollback test: no mutation occurred. The failed-stop receipt's first
  structural DLQ completion rank is corrected from the live total-count
  shorthand 69 to final completion rank 68.
- Deployment image/health: no rebuild, restart, deploy, provider call,
  projection, or activation.
- Remaining risks: exactly one unchanged-gate resume is conditionally
  authorized; any second rolling stop parks for owner. Hard-document failures,
  final ledger, tail, owner sample, projection, and activation remain open.
- Checklist boxes closed: diagnosis only. T9.3 paid completion remains open.

### 2026-07-15 - T9.3 B1 Phase-2 bounded-resume seal

- Commit: this seal commit on `claude-continuation-20260713`; resume launches
  only from the published exact bytes.
- Owner: senior approved the bounded historical-window recovery state machine
  at `COORDINATION.md#2026-07-15T09:10:30Z`; no further GO is required after
  green seal publication.
- Corpus/data scope: mark only; persisted exact 721-row selection at 148
  terminal = 141 accepted / 7 DLQ, 573 queued, zero running. Baseline hash is
  `sha256:d5c7fd3cd86ae961ec71ab5719c79020dbb489530c8bc97ab203bd69f734ab0c`.
- Code changes: add exact resume-preflight/resume modes; bind the original
  44/50 ranks 99–148 window and failure ranks; latch only that historical
  condition; preserve every other stop; require recovery by terminal 198;
  re-enable the original rolling gate after recovery; continue checkpoints at
  150; refuse execution-output/checkpoint collisions.
- Durable migration/backfill: none at seal time; no provider calls or Mongo/
  canonical writes.
- Before metrics: current runner would immediately re-observe the old rolling
  stop and overwrite checkpoint paths with zero progress.
- After metrics: recovery is bounded and exactly authorized. Original absolute
  ceiling remains `$49.4464896999999995`; current basis
  `$6.955576299999998`; maximum next reservation `$0.09536318`; the resume
  does not refresh the `$46.69` umbrella.
- Tests by tier: backend 79/79 and worker 79/79; Black and both compiles green;
  36-file host/backend/worker parity green; invalid baseline GO expected
  `EXIT=1` followed by identical green baseline; immutable failed-execution
  collision expected `EXIT=1` with SHA unchanged. Final live preflight true
  `EXIT=0`. Permanent receipt:
  `docs/T9_3_PROSE_PHASE2_RESUME_SEAL_RECEIPT_2026-07-15.md`.
- Cross-corpus test: not applicable; mark-only noncanonical lane. Canonical
  census is read-only and zero active ingest/running semantic jobs are proven.
- Failure/rollback test: missing overlay files/cards and the inappropriate
  fresh-selection-at-resume equation failed closed without provider calls or
  DB writes. Resume code is additive/revertible; purchased rows remain an
  append-only ledger.
- Deployment image/health: no rebuild/restart. Exact overlays and registries
  are byte-identical across backend and ingest-worker.
- Remaining risks: failure to recover by terminal 198 or a later rolling fall
  parks for owner; second cumulative ReadTimeout and all other original stops
  remain live. Completion, tail, owner sample, projection, and activation stay
  open.
- Checklist boxes closed: bounded-resume seal only. T9.3 paid completion
  remains open.

### 2026-07-15 - T9.3 B1 Phase-2 resume launch-fix seal

- Commit: this supplemental seal commit on `claude-continuation-20260713`;
  relaunch occurs only from the dual-published exact bytes.
- Owner: senior ruled at `COORDINATION.md#2026-07-15T09:47:55Z` that the
  zero-claim refusal left the ONE-resume authorization unconsumed and approved
  the message-free execution-stage observability seam.
- Corpus/data scope: mark only; exact persisted 721-row selection remains 141
  accepted / 7 DLQ / 573 queued / zero running. No checkpoint 0150 exists.
- Code changes: canonicalize aware and naive Mongo UTC completion instants to
  the same baseline-hash form; attach allowlisted non-secret codes to the six
  execution-only guard stages; emit no exception message.
- Durable migration/backfill: none. Failed launch and diagnostics made zero
  job, provider, or canonical writes; all `last_planned_at` values remained
  unchanged.
- Before metrics: preflight hash `d5c7fd3…`; under-lease hash `c82162c1…`
  solely because identical UTC instants serialized with versus without
  `+00:00`.
- After metrics: both paths reproduce exact baseline `d5c7fd3…`, terminal
  identity `192ffb6b…`, rolling identity `04affd4f…`, and current basis
  `$6.955576299999998`.
- Tests by tier: backend 85/85 and worker 85/85; Black and both compiles
  green; 36-file host/backend/worker parity green; zero-write boundary reaches
  materialization with identical hashes; wrong GO returns expected `EXIT=1`
  with only `error_code=exact_go_guard`; final preflight `EXIT=0`. Permanent
  receipt:
  `docs/T9_3_PROSE_PHASE2_RESUME_LAUNCH_FIX_SEAL_RECEIPT_2026-07-15.md`.
- Cross-corpus test: not applicable; this is deterministic timestamp
  normalization in a mark-only noncanonical lane.
- Failure/rollback test: the original refusal remains preserved; no checkpoint
  or paid row was created. Code is additive/revertible, and the next execution
  must use a fresh absent output path.
- Deployment image/health: no rebuild or restart; exact source overlay is
  byte-identical on backend and ingest-worker.
- Remaining risks: the authorized recovery still must satisfy the unchanged
  rolling, timeout, consecutive-DLQ, budget, and canonical-invariance gates;
  the second rolling stop parks for owner.
- Checklist boxes closed: launch-fix seal only. T9.3 paid completion remains
  open.

### 2026-07-15 - T9.3 B1 Phase-2 operational-continuation seal

- Commit: this operational-continuation seal commit on
  `claude-continuation-20260713`; continuation launches only from the
  dual-published exact bytes.
- Owner: senior ruled at `COORDINATION.md#2026-07-15T10:09:30Z` to book the
  two bounded-success exposures and continue the same recovery because the
  checkpoint-150 telemetry stop was nonrolling infrastructure drift.
- Corpus/data scope: mark only; exact persisted 721-row selection at 150
  terminal = 143 accepted / 7 DLQ, 571 queued, zero running. Continuation
  baseline is `sha256:a8f21ed25b3ebdba6946432d73f9ac5b576b7dee347b5d5e69b1696647f406f1`.
- Code changes: add explicit continuation preflight/execution modes; exact-GO
  and under-lease hashes for the 150-terminal ledger, checkpoint 0150, and
  stopped execution; preserve original performance baseline/deadline 148/198;
  advance the next writable checkpoint to 200.
- Durable migration/backfill: exact compare-and-set cost-only booking for
  ord206/207: 2 + 1 observed calls bounded at `$0.06673898` + `$0.03819987` =
  `$0.10493885`. Actual cost stays null; semantic/cache identities unchanged.
- Before metrics: cumulative accounting was incomplete after stale runtime
  telemetry recorded two successes as zero calls.
- After metrics: accounting closes with known `$6.775576299999998`, bounded
  `$0.28493884999999997`, and ceiling basis `$7.060515149999998`.
- Tests by tier: backend 83/83 and worker 83/83; Black, both compiles, and
  49-file parity green; invalid GO expected `EXIT=1`; exact-GO under-lease
  boundary and final credential-blind preflight both `EXIT=0`. Permanent
  receipt:
  `docs/T9_3_PROSE_PHASE2_OPERATIONAL_CONTINUATION_SEAL_RECEIPT_2026-07-15.md`.
- Cross-corpus test: not applicable; mark-only noncanonical paid lane.
  Protected canonical and ambient Qdrant stores are exactly unchanged.
- Failure/rollback test: compare-and-set refuses any row drift; immutable stop
  hashes are enforced twice; checkpoint 0150 cannot be overwritten; wrong GO
  emits only allowlisted `exact_go_guard`.
- Deployment image/health: no rebuild or restart. Current runner SHA
  `30968e82…` and the 49-file closure are exact in backend and worker.
- Remaining risks: recovery failure by terminal 198 or a later rolling fall
  parks for owner. Completion, tail, owner sample, projection, and activation
  remain open.
- Checklist boxes closed: operational-continuation seal only. T9.3 paid
  completion remains open.

### 2026-07-15 - T9.3 B1 Phase-2 recovery pause

- Commit: this pause receipt commit on `claude-continuation-20260713`.
- Owner: owner stop/priority order at
  `COORDINATION.md#OWNER-RELAY-2026-07-15T10:55:37Z`; the remaining digest
  queue is owner-gated and the RunPod extraction E2E is the finish line.
- Corpus/data scope: mark only; exact 721-row selection, 186 terminal = 178
  accepted / 8 DLQ, 535 queued, zero running.
- Code changes: none sealed by this receipt. A materializer preservation fix
  is locally drafted but undeployed and parked.
- Durable migration/backfill: exact restoration of the already-authorized
  ord206/207 bounded-success fields after materialization temporarily
  reclassified them as zero-cost cache hits; 2/2 CAS, zero provider calls.
- Before metrics: recovery baseline 150 terminal, rolling 44/50.
- After metrics: recovery reached terminal 159; final rolling 48/50; post-
  baseline 35/36 accepted; stopped on second cumulative ReadTimeout.
- Tests by tier: stop diagnostic `EXIT=0`; execution expected `EXIT=1` on live
  guard; budget accounting complete; protected canonical stores exact.
  Permanent receipt:
  `docs/T9_3_PROSE_PHASE2_RECOVERY_PAUSE_RECEIPT_2026-07-15.md`.
- Cross-corpus test: not applicable; mark paid lane. Ambient
  `hermes_memories` +2 disclosed separately.
- Failure/rollback test: zero running, no checkpoint 0200, no retry of ord242,
  and 535 queued rows remain durable.
- Deployment image/health: no rebuild/restart during the paid run.
- Remaining risks: future resume requires an owner line and a sealed
  materializer preservation fix. T9.3 paid completion remains open.
- Checklist boxes closed: pause receipt only.

### 2026-07-15 - P2.7 RunPod finish-line preregistration freeze

- Commit: this preregistration commit on
  `claude-continuation-20260713`.
- Owner: owner-authorized full RunPod finish line; full chain pre-authorized
  by the senior in `COORDINATION.md`.
- Corpus/data scope: read-only 75-file owner source inspection for filename,
  size, hash, and preregistered evidence anchors; no corpus created yet and
  existing corpora received zero writes.
- Code changes: deterministic balanced filename-TF-IDF selector, retrieval
  preregistration verifier, and pinned-local same-chunk reference runner.
- Durable migration/backfill: none.
- Before metrics: unconstrained topic clustering failed its representation
  gate; first balanced execution failed on a Python 3.9 compatibility-only
  keyword.
- After metrics: 15/15 frozen source hashes exact; deterministic selection is
  byte-identical on repeat at `da7b94c1…`; 17 queries × 3 tiers = 51 frozen
  retrieval executions; zero evidence-anchor misses.
- Tests by tier: preregistration covers Qdrant-only, Qdrant+Mongo, and
  Qdrant+Mongo+Graph; final verifier, selector repeat, compile, Black, and diff
  gates all return true `EXIT=0`.
- Cross-corpus test: four relationship/multi-document queries are frozen;
  corpus-boundary and citation-membership gates are both 1.0.
- Failure/rollback test: targets and thresholds freeze before first query;
  new-corpus-only identity and zero-existing-write rules fail closed.
- Deployment image/health: no inference, image promotion, endpoint operation,
  provider call, or deployment occurred.
- Remaining risks: local reference, immutable build inspection, blue-green
  canary, live same-chunk parity, fresh-corpus ingest, and summary spend
  ceiling remain open. Permanent receipt:
  `docs/RUNPOD_FINISH_LINE_PREREGISTRATION_RECEIPT_2026-07-15.md`.
- Checklist boxes closed: none; this freezes inputs for the open P2.7 gates.

### 2026-07-15 - P2.7 custom-image B0 feasibility seal

- Commit: this custom-image source commit on
  `claude-continuation-20260713`.
- Owner: full RunPod finish line authorized; exact-image remediation follows
  the senior's preferred-route ruling after Flash B2 failed.
- Corpus/data scope: source/build validation only; zero corpus or durable-store
  writes.
- Code changes: standalone RunPod queue envelope on the existing strict worker,
  immutable-base Dockerfile, hashed 147-distribution lock, in-image model bake
  verifier, and custom-image contract verifier.
- Durable migration/backfill: none.
- Before metrics: Flash's tar excludes torch and its immutable base attests
  Python 3.12 / torch 2.9.1, not the certified runtime.
- After metrics: exact 13 critical/runtime pins, 147 hashed distributions,
  source closure `41a2c0db…`, zero secret findings, non-root/offline model
  contract, warning-free Docker check.
- Tests by tier: backend deterministic spine 50/50; worker 6/6; standalone
  handler 3/3; closure and custom contract verifiers true `EXIT=0`.
- Cross-corpus test: not applicable to the source seal; frozen 15-document E2E
  remains downstream.
- Failure/rollback test: malformed custom envelopes fail closed; the original
  Flash artifact remains rejected and no blue endpoint changed.
- Deployment image/health: no image built/pushed and no endpoint operation.
- Remaining risks: build-time CUDA-13 closure and runtime GPU behavior require
  local image attestation plus the live canary; registry push is an external
  publication boundary. Permanent receipt:
  `docs/RUNPOD_CUSTOM_IMAGE_B0_FEASIBILITY_RECEIPT_2026-07-15.md`.
- Checklist boxes closed: none; B2 remains open until image/digest attestation.

### 2026-07-15 - P2.7 same-chunk B1 pinned-local reference

- Commit: this receipt commit on `claude-continuation-20260713`; executed from
  published source commit `08385fa`.
- Owner: owner-authorized full RunPod finish line; this is the frozen local
  comparison half.
- Corpus/data scope: 12 preregistered synthetic extraction tasks; zero corpus
  reads or writes.
- Code changes: none after the published preregistration runner.
- Durable migration/backfill: none.
- Before metrics: live green output does not yet exist; no parity claim.
- After metrics: both local normalized output hashes are identical at
  `e84e2e3d…`; 126 entities, 56 predicates, 14 windows, zero relations.
- Tests by tier: two complete runs, byte determinism, exact runtime/model/
  registry/source identities, and zero provider/database/graph/vector writes;
  true `EXIT=0`.
- Cross-corpus test: not applicable to same-chunk extraction parity.
- Failure/rollback test: comparison contract requires zero missing, extra, or
  semantic mismatches and exact threshold-side selection in the live gate.
- Deployment image/health: pinned-local only; no endpoint operation.
- Remaining risks: live green endpoint must reproduce the reference across all
  12 tasks. Permanent receipt:
  `docs/RUNPOD_SAME_CHUNK_B1_LOCAL_REFERENCE_RECEIPT_2026-07-15.md` and frozen
  baseline `docs/baselines/RUNPOD_SAME_CHUNK_LOCAL_REFERENCE_2026-07-15.json`
  (SHA `7615ad23…`).
- Checklist boxes closed: none; live same-chunk parity remains open.

### 2026-07-15 - P2.7 custom-image B2 local bake and attestation

- Commit: this receipt commit on `claude-continuation-20260713`; image inputs
  are the already-published commit `08385fa`.
- Owner: full RunPod finish line authorized; senior ruling at 12:34:54Z
  authorizes registry publication only after private visibility is verified.
- Corpus/data scope: local Docker build and read-only in-image inspection;
  zero corpus, provider, or durable-store writes.
- Code changes: none after the published B0 source contract.
- Durable migration/backfill: none.
- Before metrics: Flash's platform artifact was rejected because it could not
  attest the locked Python/torch runtime or an immutable deploy identity.
- After metrics: local linux/amd64 image is 5,864,789,051 bytes; Python
  3.11.15; all 13 critical/runtime distributions exact; model, registry, and
  13-file source closure hashes exact; non-root UID 10001.
- Tests by tier: image build, selected config inspection, full in-image bake
  report, torch import, and malformed handler-envelope checks all true
  `EXIT=0`.
- Cross-corpus test: not applicable to the local image gate; frozen 15-document
  E2E remains downstream.
- Failure/rollback test: three malformed envelope classes reject fail-closed;
  no registry or endpoint mutation occurred, so existing blue endpoints are
  unchanged.
- Deployment image/health: local manifest list `d3620d85…`; linux/amd64
  manifest `ef7a286d…`; config `6fa4eeb5…`. These are local build identities,
  not a claimed registry digest. GPU health remains a live-canary gate.
- Remaining risks: registry push/digest, CUDA-13 GPU compatibility,
  blue-green endpoint deploy, canary, and same-chunk parity remain open.
  Permanent receipt:
  `docs/RUNPOD_CUSTOM_IMAGE_B2_LOCAL_ATTESTATION_RECEIPT_2026-07-15.md`.
- Checklist boxes closed: none; remote B2/B3 remain open.

### 2026-07-15 - P2.7 custom-image B2 private remote identity / B3 quota stop

- Commit: this receipt commit on `claude-continuation-20260713`; image source
  is published commit `08385fa`.
- Owner: full RunPod finish line authorized; senior explicitly restricted
  registry publication to private visibility.
- Corpus/data scope: private Docker Hub publication and RunPod control-plane
  template/credential operations only; zero corpus/provider/durable-store
  writes.
- Code changes: none; operator helper remained untracked in `tmp/` and was
  never staged.
- Durable migration/backfill: none.
- Before metrics: repository absent; no RunPod private-registry credentials;
  both accounts allocated worker maxima 2 embed + 8 blue extraction.
- After metrics: repository private; remote index `d3620d85…` and amd64 child
  `ef7a286d…` exactly match local; two private-pull credentials stored with
  zero secret values emitted; green endpoint count remains zero.
- Tests by tier: authenticated/unauthenticated visibility, image config/history
  secret scan, push true exit, raw OCI digest/child rehash, account credential
  postcheck, and unchanged-blue census.
- Cross-corpus test: blocked behind B3; not run.
- Failure/rollback test: primary endpoint create correctly failed at RunPod's
  quota gate, true `EXIT=1`; both blue extraction endpoints/config are
  unchanged and no canary ran.
- Deployment image/health: exact inert primary template `zepw9ehfnj`; no green
  endpoint and no health claim.
- Remaining risks: B3 requires quota increase, third account, or explicitly
  authorized capacity reallocation. Permanent receipt:
  `docs/RUNPOD_CUSTOM_IMAGE_B2_REMOTE_PUBLICATION_RECEIPT_2026-07-15.md`.
- Checklist boxes closed: B2 remote image identity complete; B3 remains open.

### 2026-07-15 - P2.7 B3 primary green control-plane deployment

- Commit: this runner/receipt commit on `claude-continuation-20260713`.
- Owner: full finish line pre-authorized; senior approved primary-only embed
  max 2→1 reallocation with mandatory restore at cutover-or-abort.
- Corpus/data scope: RunPod control plane only; no inference, provider, corpus,
  or durable-store write.
- Code changes: credential-blind green canary/parity/retry runner plus four
  focused comparator/refusal tests.
- Durable migration/backfill: none.
- Before metrics: green create quota rejection at 2 embed + 8 blue extraction;
  green endpoint count zero.
- After metrics: primary embed exact except max=1; green endpoint
  `whs9pjd34h2hs2`, template `zepw9ehfnj`, workers 0..1; blue unchanged;
  secondary untouched.
- Tests by tier: runner 4/4; compile, Black, diff true `EXIT=0`; post-mutation
  GraphQL identity/config checks true `EXIT=0`.
- Cross-corpus test: downstream after canary/parity; not run here.
- Failure/rollback test: rejected quota and partial-field update candidates
  retained; helper caught default drift and full-field correction restored it.
  Mandatory embed max restore command is sealed for cutover-or-abort.
- Deployment image/health: control-plane GREEN only; first image pull/GPU
  inference remains B4.
- Remaining risks: 5.86 GB private pull/cold start, CUDA-13 GPU compatibility,
  B4 canary, B5 parity, B6 retry. Permanent receipt:
  `docs/RUNPOD_CUSTOM_IMAGE_B3_GREEN_DEPLOY_RECEIPT_2026-07-15.md`.
- Checklist boxes closed: B3 green deploy; health/canary remains open.

### 2026-07-15 - P2.7 B4 first-canary failure, rollback, and corrective seal

- Commit: this corrective source/receipt commit on
  `claude-continuation-20260713`.
- Owner: full finish line pre-authorized; senior accepted the abort rollback,
  required provider job-ID persistence before await, and pre-authorized
  evidence-gated redeploy after remedy.
- Corpus/data scope: one live 12-task green request followed by read-only local
  exact-image diagnosis; no corpus, provider-setting, graph, vector, or
  database write.
- Code changes: exact baked-cache runtime env binding, bake/runtime path
  containment assertion, enforce-runtime image probe, and fsynced pre-await
  provider job journal in the B4 runner.
- Durable migration/backfill: none.
- Before metrics: green job queued→initializing→`FAILED` after approximately
  19 minutes; no output envelope; original runner failed to retain job ID.
- After metrics: exact-image failure reproduced before inference; corrected-env
  exact-image probe returns one result / two entities / one predicate / zero
  relations in 20.43 seconds under amd64 emulation, with zero provider calls
  and durable writes.
- Tests by tier: runner 5/5; image contract all green with 147 exact locked
  distributions and zero secret findings; exact-image corrected-env probe,
  Black, compile, and diff checks true `EXIT=0`.
- Cross-corpus test: not run; dependent chain stopped before B5/B6 and the
  fresh 15-document corpus.
- Failure/rollback test: green endpoint deleted; primary embed restored max
  1→2 with all other sealed fields unchanged; both extraction blues unchanged;
  secondary untouched; final census true `EXIT=0`.
- Deployment image/health: old private digest remains failed for B4 and will
  not be redeployed. Corrected source is not yet built/published; inert template
  and private registry-auth records allocate no workers.
- Remaining risks: corrected immutable build must pass the runtime probe
  without an injected env override; remote digest, GPU canary, same-chunk
  parity/retry, fresh-corpus E2E, and retrieval eval remain open. Permanent
  receipt:
  `docs/RUNPOD_CUSTOM_IMAGE_B4_FIRST_CANARY_FAILURE_RECEIPT_2026-07-15.md`.
- Checklist boxes closed: none; B4 remains open.

### 2026-07-15 - P2.7 B4 corrected rebuild and private publication

- Commit: corrective image source `8708f37`; this receipt/runner-identity
  commit on `claude-continuation-20260713`.
- Owner: full finish line pre-authorized; evidence-gated redeploy approved by
  senior after the first-canary failure class was identified and remedied.
- Corpus/data scope: local image build/probe plus private registry publication;
  zero RunPod endpoint, provider-setting, corpus, graph, vector, or database
  mutation.
- Code changes: green runner discovery identity advanced to the corrected image
  name; no extraction/retrieval semantic change.
- Durable migration/backfill: none.
- Before metrics: old image deterministically could not locate its baked model
  offline.
- After metrics: corrected 5,864,789,730-byte image runs real offline inference
  without override; private remote index `c03416dc…` and amd64 child
  `2bdb966e…` equal local.
- Tests by tier: source closure, static Docker, 147-package contract, build,
  full asset/package attestation, no-override runtime probe, malformed handler
  refusals, private-visibility preflight, push, and raw-OCI rehash all true
  `EXIT=0`.
- Cross-corpus test: not run; remains downstream of B4/B5/B6.
- Failure/rollback test: no external compute/control-plane mutation occurred;
  both blue endpoints and restored embed capacity remain untouched.
- Deployment image/health: immutable corrected image is private and deployable;
  live GPU health is not yet claimed.
- Remaining risks: green deploy, live GPU canary, same-chunk parity/retry,
  fresh-corpus E2E, and retrieval eval. Permanent receipt:
  `docs/RUNPOD_CUSTOM_IMAGE_B4_CORRECTED_REBUILD_RECEIPT_2026-07-15.md`.
- Checklist boxes closed: corrected B2 identity only; B4 remains open.

### 2026-07-15 - P2.7 B4/B5 instrumented parity failure and rollback

- Commit: harness preflight commit `ac8bc4a`; this terminal RED receipt commit
  on `claude-continuation-20260713`.
- Owner: one instrumented canary authorized after senior ruled the first
  uninstrumented orphan diagnostic-only; frozen gates remained absolute.
- Corpus/data scope: two green requests total (one diagnostic-only orphan, one
  instrumented valid job); zero provider-setting, corpus, graph, vector, or
  database write.
- Code changes: journal writability is now fsynced before provider dispatch;
  unwritable paths refuse before any HTTP call.
- Durable migration/backfill: none.
- Before metrics: corrected image/runtime probe green; first live harness path
  was not writable and therefore not a valid bar execution.
- After metrics: instrumented job completed with 1,780ms delay / 1,196ms
  execution. Functional semantics: 12 chunks, 126 entities, 56 predicates,
  2/2 temporal phrases, zero relations/missing/semantic mismatches. Confidence
  max delta `0.0001373291015625` exceeds frozen `0.00001`; 81/126 values fail.
- Tests by tier: runner 6/6; actual-container-user journal preflight,
  functional canary diagnostic, confidence diagnostic, status preservation,
  and rollback census true `EXIT=0`; instrumented gate correctly `EXIT=1`.
- Cross-corpus test: not run; B5 failure stops B6, fresh-corpus E2E, and
  retrieval evaluation.
- Failure/rollback test: job ID survived; full output preserved; green deleted;
  primary embed restored max 1→2; both blues unchanged; secondary untouched.
- Deployment image/health: corrected image boots and serves, but required
  pinned-local confidence parity is not green; no production/cutover claim.
- Remaining risks: device-level confidence variation is inferred, not proven;
  tolerance/contract changes require explicit respecification and were not
  attempted. Permanent receipt:
  `docs/RUNPOD_B4_B5_PARITY_FAILURE_RECEIPT_2026-07-15.md`.
- Checklist boxes closed: none; P2.7 production readiness remains open. Senior
  subsequently authorized a narrow versioned deterministic-runtime rebuild
  and one unchanged-tolerance retest; that remediation is not a gate pass.

### 2026-07-15 - P2.7 deterministic B4/B5/B6 lockdown green

- Commit: deterministic source `3b66f55`; control runner correction `5cc4199`;
  900-second/warmth transport correction `fce624d`; permanent receipt in this
  publication commit on `claude-continuation-20260713`.
- Owner: owner finish-line order and senior full-chain pre-authorization;
  frozen parity/refusal gates were not weakened.
- Corpus/data scope: 12 frozen same-chunk tasks plus three invalid controls;
  no corpus, graph, vector, or database write.
- Code changes: versioned deterministic worker profile; invalid-control
  provider-status validation; fsynced job/case receipts; 900-second control
  transport patience and pre-submit warmth journaling.
- Durable migration/backfill: none.
- Before metrics: nondeterministic image max confidence delta
  `0.0001373291015625` (RED); first control retry encountered provider capacity
  and produced no contract verdict.
- After metrics: 12/12 results, 126 controlled entities, 56 predicates, both
  temporal phrases, zero relations/missing/semantic mismatches; confidence max
  delta `2.384185791015625e-06` under `1e-05`. All three controls return exact
  named refusal. Independent replay has exact results and confidence delta 0.
- Tests by tier: runner focused 10/10; Black, compile, diff, local image probe,
  live canary, replay compare, semantic-hash compare, and final controls true
  `EXIT=0`.
- Cross-corpus test: not yet run; fresh 15-document E2E is next after B7.
- Failure/rollback test: earlier failed deployments were deleted and primary
  embed restored. Current green is retained scale-to-zero after GREEN B4–B6;
  rollback remains delete-green + restore embed max 2.
- Deployment image/health: immutable private digest `4cb08457…`; current green
  `hk81nfl5cnwufx`; both invalid-control warmth probes ready 1/throttled 0.
- Remaining risks: B7 real-ingest adapter/cutover is contract-RED because the
  published production adapter accepts only legacy v2/v3; no production-ready
  or corpus-scale claim. Permanent receipt:
  `docs/RUNPOD_DETERMINISTIC_LOCKDOWN_B4_B6_RECEIPT_2026-07-15.md`.
- Checklist boxes closed: pinned endpoint artifact and retry-safety evidence
  are satisfied for the locked small gate; the parent P2.7 production-ready
  acceptance remains open until B7, fresh-corpus E2E, and scale/readiness work.

### 2026-07-17 - MLX-EVAL-STABILITY-V1

- Commit: this review-branch receipt commit on
  `codex/mlx-embedder-stability-20260717`.
- Owner: Step 0 of the 2026-07-17 owner mission; Step 0.5 routing remains a
  separate parity-gated directive.
- Corpus/data scope: read-only embedder probes and fixed synthetic soak text;
  zero corpus-store mutation.
- Code changes: 30-second query-plan embed deadline, one timeout retry,
  120-second pooled keepalive, fail-closed health/warmup endpoint, preflight
  command wrapper, and sustained-soak harness.
- Durable migration/backfill: none.
- Before metrics: two frozen regression runs had died at the outer 5-second
  embedding timeout under sustained load.
- After metrics: 100/100 calls, zero failures, 2.839-second wall time,
  2,113.748 requests/minute, 1024 dimensions.
- Tests by tier: isolated focused/adjacent 24 passed; canonical deployed
  backend 15 passed; runtime contract and soak true `EXIT=0`.
- Cross-corpus test: not applicable; no retrieval or corpus data changed.
- Failure/rollback test: unit test proves a refused preflight never launches
  the eval command; canonical five-overlay recreate is the runtime rollback.
- Deployment image/health: temporary review image `fddb3eae...` healthy
  against host MLX `:8082`; post-soak health ready, idle, queue 0, no error.
- Remaining risks: client stability does not authorize mixed embedding
  provenance. Owner-directed Step 0.5 must pass its preregistered Mac/RunPod
  parity canary or fall back to serialized Mac evaluation.
- Checklist boxes closed: `MLX-EVAL-STABILITY-V1`.

### 2026-07-17 - T-QUERY-ROUTING-V1 frozen regression rerun

- Commit: this receipt commit on `codex/temporal-regression-20260717`;
  temporal implementation `1db5e5b`; MLX stability dependency `d29e8ae`.
- Owner: owner-authorized temporal frozen regression after Step 0 stability;
  Step 0.5 RunPod routing is explicitly held and was not built.
- Corpus/data scope: immutable 15-document E2E corpus, read-only retrieval;
  no corpus mutation.
- Code changes: no temporal logic change; integrated the independently sealed
  30-second timeout, retry-once, pooled warmup, and pre-batch health gate.
- Durable migration/backfill: none.
- Before metrics: temporal diagnostic already 0.9583 document hit / 0.8750
  full-anchor; frozen no-regression was unverified after MLX outages.
- After metrics: OFF→ON direct 1.00→1.00, lay 1.00→1.00, relationship
  0.75→0.75, technical/corpus/citation/effective-tier all 1.00→1.00.
  The separate negative gate remained RED at 0.5556→0.4444.
- Tests by tier: all 51 executions in each arm across Qdrant-only,
  Qdrant+Mongo, and Qdrant+Mongo+Graph; 63 focused/adjacent tests passed.
- Cross-corpus test: selected-corpus boundary and citation membership both
  remained exactly 1.00 in both arms.
- Failure/rollback test: both evals were preflight-gated and had zero embedding
  or transport failures; canonical five-overlay backend restore is healthy and
  the temporal setting is absent/default OFF.
- Deployment image/health: paired arms used the same immutable image
  `sha256:c325ed49...`; each preflight returned ready/1024-d/queue-zero;
  canonical runtime restored at `7233077`.
- Remaining risks: the frozen negative-refusal gate remains a separate RED;
  temporal activation is not authorized by this dark-ship promotion receipt.
  Permanent receipt:
  `docs/TEMPORAL_QUERY_ROUTING_FROZEN_REGRESSION_2026-07-17.md`.
- Checklist boxes closed: `T-QUERY-ROUTING-V1-RERUN` only; parent
  `T-QUERY-ROUTING-V1` remains open until review/merge policy closes it.

## Implementation Log Template

Copy this section for every completed item:

```markdown
### YYYY-MM-DD - Checklist ID and title

- Commit:
- Owner:
- Corpus/data scope:
- Code changes:
- Durable migration/backfill:
- Before metrics:
- After metrics:
- Tests by tier:
- Cross-corpus test:
- Failure/rollback test:
- Deployment image/health:
- Remaining risks:
- Checklist boxes closed:
```

## Completion Rule

Do not close a parent section while any required sub-item or acceptance check is
open. If an item is intentionally rejected, record the decision, evidence, and
replacement approach in the implementation log rather than silently deleting
the checkbox.

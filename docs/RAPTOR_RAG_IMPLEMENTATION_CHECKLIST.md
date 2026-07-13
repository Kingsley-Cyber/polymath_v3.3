# RAPTOR RAG Implementation Checklist

Last updated: 2026-07-13  
Baseline commit: `d3159b8`  

## Execution Order — SEQUENCED PLAN OF RECORD

The dependency-ordered execution plan derived from this ledger is
`docs/PLAN_CRITIQUE_2026-07-13.md` (S0–S14, with verified data-state receipts
and the critique of prior sequencing). Execute in S-order; a step's checklist
anchors are listed per row. This ledger stays the item-level source of truth;
the plan file is its ordering.

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
full "universal evidence packet" architecture now (overlapping pieces are
already tracked as shelves/cards/bridges/sufficiency/receipts; the novel
layers — epistemic ledger, artifact blueprint, domain compilers,
post-synthesis usage ledger — are recorded as the post-checklist synthesis
packet v2 design, not scope for this pass).

Tracked work added by this audit:

### P0.8 Schema Enforcement At Storage Boundaries

- [x] Add additive Mongo JSON-schema validators (warn-first, then enforce)
  for documents, parent_chunks, ghost_b_extractions, corpus_lexicon, and
  summary_tree. *(merged; validators applied warn-mode live 2026-07-13)*
- [ ] Enforce typed-model acceptance at the Mongo writer boundary (close the
  B0 "writers accept ONLY typed models" gap) without breaking existing
  callers.
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
  Phases 2-7 — source versions/episodes/assertions + outbox, Qdrant payload
  indexes + projection without re-embedding, versioned Neo4j `RELATES_TO`
  edges (executed together with P2.5 typed-signature work — same edge-schema
  migration), query temporal modes (CURRENT/AS_OF/BETWEEN/AS_KNOWN/EVOLUTION),
  ONE eligibility service across Tier-0/Fast/Hybrid/Graph, shadow-then-enforce,
  synthesis temporal receipts, and capability-specific readiness
  (`temporal_unavailable|partial|strict_ready` — same seam as the
  operational-vs-metadata-quality readiness split adopted in Audit Delta 2).
- Ordering rationale: field capture rides in-flight generation/backfills for
  free; retrieval-behavior changes are gated by the held-out suite; temporal
  eligibility needs the fields to exist first; re-extracting before the
  contract hook would force a second paid extraction pass.

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
- [ ] Optionally materialize `transfer_edge` rows as a pure evidence join:
  shared principle/mechanism across documents with different central subjects.
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

### P2.3 Add Query-Only Embedding Instructions

- [ ] Introduce a query-only embedding method distinct from document/concept/
  tree embedding.
- [ ] Make instructions model-specific and versioned.
- [ ] Keep raw query plus instruction version in the cache key.
- [ ] A/B test Qwen3 query instructions against the held-out suite.
- [ ] Do not re-embed documents unless the evaluation proves a document-side
  contract change is required.

Acceptance:

- [ ] Recall improves without corrupting document-vector compatibility.

## P2 - Extraction And RunPod Parity

### P2.4 Negation And Relation Correctness

- [ ] Preserve negation tokens during evidence-overlap validation.
- [ ] Add `negated` and evidence-sentence boundaries to the relation artifact.
- [ ] Parse only emitted relation evidence sentences unless benchmarks justify
  full-document parsing.
- [ ] Ship and pin any required spaCy model in the RunPod contract.
- [ ] Decide promotion policy for negated relations; retain evidence for audit.

### P2.5 Typed Relation Signatures

- [ ] Build a reviewed predicate domain/range compatibility table from accepted
  real edges.
- [ ] Initially annotate `signature_valid` and violation reason; do not drop or
  remap edges automatically.
- [ ] Measure violation rates by provider/model/corpus/predicate.
- [ ] Promote hard/soft enforcement only after false-positive review.

### P2.6 Engine Parity And Provenance

- [ ] Version a shared extraction artifact contract across cloud, local, and
  RunPod engines.
- [ ] Record extraction engine, model ID, contract hash, field-level methods,
  offsets, confidence, and evidence.
- [ ] Decide whether deterministic facts are supported per engine; do not make
  noisy facts mandatory for queryability.
- [ ] Leave `object_kind` blank when it cannot be grounded; do not fabricate it
  from coarse entity type.
- [ ] Add relation-cue derivation only when source-evidenced.
- [ ] Keep deterministic alias/definition rules in shared backend validation.
- [ ] Run the same post-extraction lexicon projector (co-occurrence, usage
  frames, semantic profile, DF/specificity, and representation admission) for
  cloud, local, RTX, and RunPod artifacts so provider choice cannot change the
  query-translation contract.

### P2.7 RunPod Production Validation

- [x] RunPod Flash contract, settings, backend adapter, and worker code exist.
- [ ] Deploy a pinned endpoint artifact.
- [ ] Pass canary, 100, 500, and 5,000-chunk yield/quality/budget gates.
- [ ] Compare entity, relation, evidence, ontology, graph-promotion, and failure
  rates against current cloud/local paths.
- [ ] Verify retries never erase valid summaries, vectors, or graph artifacts.
- [ ] Wire extraction/concept readiness into corpus strict-readiness.

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

## P3 - Thematic RAPTOR And Cross-Corpus Bridges

### P3.1 Pilot Multi-Theme Semantic Routing

- [ ] Start with `markbuildsbrands_transcripts` or another small healthy corpus.
- [ ] Snapshot/scroll existing parent vectors without re-embedding.
- [ ] Compare clustering methods and stability across corpus epochs, including
  soft/non-exclusive membership rather than forcing each parent into one
  exclusive thematic cluster.
- [ ] Allow one parent, section, document, and concept to belong to multiple
  evidence-backed themes.
- [ ] Allow one query and each of its sub-queries to activate multiple themes
  within and across selected corpora.
- [ ] Generate evidence-backed summaries for themes with member parent IDs,
  membership strength, and source provenance.
- [ ] Index themes with explicit abstraction level, version, corpus ownership,
  and soft member bindings.
- [ ] Fuse evidence across activated themes; never use a single thematic winner
  as a hard gate over all downstream retrieval.
- [ ] Use themes as routers first; allow synthesis use only with hydrated
  evidence.
- [ ] Diagnose themes activated, themes covered, themes unsupported, and
  contributions by activated themes to final evidence.
- [ ] Evaluate broad recall, precision, storage, update cost, and latency.

Acceptance:

- [ ] Multiple relevant themes can survive routing and final evidence selection.
- [ ] Themes materially improve broad/cross-document questions.
- [ ] Focused questions do not lose source precision.
- [ ] Only after success may the architecture claim a thematic RAPTOR layer.

### P3.2 Materialize Cross-Corpus Bridge Cards

- [ ] Select only evidence-strong entities/relations spanning multiple corpora.
- [ ] Exclude generic/high-degree `RELATES_TO` hubs without typed support.
- [ ] Store bridge concepts, source corpora, documents, predicates, confidence,
  and evidence IDs.
- [ ] Use bridge cards for shelf/document routing, not unsupported final claims.
- [ ] Evaluate non-obvious cross-domain recall and false-bridge rate.
- [ ] Materialize deterministic `transfer_edge` rows when documents with
  meaningfully different central subjects share a specific validated principle
  or mechanism.
- [ ] Store endpoint document/concept IDs, principle/mechanism ID, evidence IDs
  on both sides, support counts, specificity, temporal scope, and policy
  version.
- [ ] Do not create transfer edges from embedding proximity alone.

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

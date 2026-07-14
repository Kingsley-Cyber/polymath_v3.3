# BUILDLINE — the temporal north-star (authoritative build order)

**What this is:** the single time-ordered checkpoint line from the repo's
current state to Definition of Pass. Item truth = the checklist
(/Users/king/polymath_v3.3/docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md);
architectural truth = the design docs; temporal truth = THIS file.

**Derivation rule (owner correction 2026-07-14):** this file is DERIVED FROM
THE CHECKLIST ON DISK, never from any agent's memory. Every checklist section
with open work maps to exactly one checkpoint in the COVERAGE MAP below;
`scripts/check_buildline_coverage.py` verifies the mapping and fails loudly on
any unmapped section. Every newly adopted design claims a slot the same day
(UNSLOTTED list = planning defects).

**Maintenance:** senior updates NOW pointer + stamps; executor stamps status in
receipts; owner vetoes via COORDINATION.md. Precedence: OWNER >
CONTINUATION_HANDOFF §Decision authority > this order > daily directives.

**Model note (owner ruling 2026-07-14):** NO embedding/reranker model change is
planned. Qdrant binary quantization (~32-40x memory compression with
oversampling + full-vector rescoring, already implemented and promotion-gated)
is COMMITTED work on the CURRENT 1024-dim vectors — clone-first, recall-A/B
gated, at CP6. Any 4B-model move is owner-initiated only and has no slot.

---

**NOW → CP1** (Phase A: g1 caught the digital-PDF structural-lane bypass;
fix CP1-D1 with executor; then full g1–g10 re-run)
**UNSLOTTED:** none as of 2026-07-14 (verified by coverage script)

---

## CP0 — Foundation ✅ DONE 2026-07-14
S0–S3 landed · 3-tier regression (negatives 5/5; scorer v4) · 8 owner
registries + loader/resolver + hash taxonomy (31 tests) · glide authority.

## CP1 — Deployment validation (Phase A) ⏳ ACTIVE
g1 FINDING: `_parse_pdf_fast_text` bypass → digital PDFs lose structure.
CP1-D1: text-layer PDFs → docling layout (no OCR) → markdown/sections →
structural lane; OCR only for image-only scans; fallback counter; general
logic only. Exit: g1–g10 green on re-run. Consumes: REBATCH_RUNBOOK §Phase A.

## CP2 — Mark-only enrichment (Phase B rescoped) + P0.1 closure
Mark full regen → lexicon → cards LAST → readiness → after-eval. Ecom REMOVED
(fix→owner reingest decision→ONE pass). P0.1's 4 verify boxes ride these
receipts. Exit: coverage censuses + reconcile + no-regression eval.
Authority: 2h GLIDE after CP1. Checklist: P0.1.

## CP3 — Envelope + identity (P2.5b completion, P0.8 closure)
Identifier recipes + golden vectors · legacy adapters · projection outbox ·
ProjectionManifests (embedding_profile incl. instruction_version) · Mongo
validators · typed writer-boundary acceptance (P0.8 last box) · UGO
annotate-only canary. Exit: P2.5b acceptance. Checklist: P2.5b, P0.8.

## CP4 — Structured-output gateway (P2.5c)
SemanticDigestV1 + capability ladder + semantic validator + targeted repair +
dead-letter + provenance/cache; UGO 10-packet canary. Checklist: P2.5c.

## CP5 — Vocabulary/alias/instruction layer (S5 + P1.7 + P2.1 + P2.3 + Librarian Ph.3)
ONE versioned alias registry absorbing the 3 existing stores · qdrant latent
payload-whitelist fix · dual entity_id reconcile · P1.7 remainder
(gather-before-fanout, vector reuse, per-corpus batching, lexical-only
baseline) · P2.1 concept-contract fields (DF/specificity, senses, salience,
slim payloads) · P2.3 versioned Qwen3 instructions — the APPROVED universal
instruction's ISOLATED A/B vs baseline_live_v0 (flip = GLIDE 4h) · Librarian
Phase 3 hardening. Checklist: P1.7, P2.1, P2.3, Librarian Phase 3.

## CP6 — Repo-level RAG hygiene + Qdrant optimization (the "outside the
refactors" work, owner-flagged 2026-07-14)
P0.2 hierarchy repair remainder · P0.4 contradiction-check scope · P0.5
chunk/metadata hygiene remainder · P0.6 lease/reconciliation remainder ·
Librarian Phase 0 trustworthy-catalog reconcile · **P1.9 Qdrant hot-path audit
(20 items: per-stage timing, payload selectors, batching, filter/29-index
audit, prevent_unoptimized, topology, image pin, priority classes, keepalive)
· P3.4 QUANTIZATION — the owner's 40x: binary quantization on current
vectors, clone-first, oversampling+rescore, recall A/B, then promote** ·
v2-naive +68k reconciliation via manifests. Checklist: P0.2, P0.4, P0.5,
P0.6, Librarian Phase 0, P1.9 hot-path, P3.4.

## CP7 — Librarian measurement moment (S8)
Pair/mark card rebuild FINAL · P1.4 shelf routing · P1.5 remainder ·
Librarian Phases 1–2 milestones · P0.3 cross-corpus acceptance · P1.1
remainder (librarian rubric, automated contamination check, standing gate) ·
shelf_reserve A/B (flip = GLIDE). Checklist: P1.4, P1.5, Librarian Ph.1/2,
P0.3, P1.1.

## CP8 — Claim spine on canary (P2.4/P2.5/P2.5a)
LocalExtractionV1 + Python claim compiler on UGO · negation (P2.4) · typed
signatures (P2.5) · unified claim/frame contract (P2.5a) · C2 GLiREL
re-benchmark decides Stage-4. Checklist: P2.4, P2.5, P2.5a.

## CP9 — Semantic pipeline + extraction program on the pair (S11)
Staged pipeline via gateway (domains/frames/motifs/latent-v2 corroborating
interim-v1) · motif matcher (sequence-tolerance + role-threading recipes,
dual-score MotifCandidate) · P2.6 engine parity · P2.7 remainder (5,000 gate,
parity compare, retry safety) · P2.7b burst orchestration · P2.8 concept→doc
grounding · ecom reingest-after-fix + its ONE enrichment pass (post owner §8).
Checklist: P2.6, P2.7, P2.7b, P2.8, Temporal Program hooks.

## CP10 — Retrieval activation family-by-family (P2.2 + P2.2c + P1.2 + Librarian Ph.4)
Representation points (context-enriched children etc.) · mode→family matrix ·
lineage dedupe · rank fusion · P1.2 grounded-planner activation · Librarian
Phase 4 LLM amplification (planner-when-thin, explain-not-select). EACH
family/behavior behind its own A/B. Exit: the preregistered program target —
lay-language/cross-corpus doc-hit +10pts, no material regression.
Checklist: P2.2, P2.2c, P1.2, Librarian Phase 4.

## CP11 — Time, anchors, shapes, rerank serving
P1.3 conversation/open-book anchoring · P1.6 answer-shape routing · T-MAIN
temporal phases (assertions/episodes/eligibility/query modes) · P1.9
adaptive reranking + P3.5 reranker serving alternatives (cascade experiments;
NO model commitment). Checklist: P1.3, P1.6, Temporal Program (T-MAIN),
P1.9 adaptive rerank, P3.5.

## CP12 — Experiments + closure
P3.1 thematic routing pilot · P3.2 bridge cards · P3.3 collection
consolidation · Quick Upload decision · Non-Negotiable Invariants final sweep
· Required 16×3 regression matrix · Definition of Strict Ready per corpus ·
restart/rollback/concurrency sweep · deploy-from-main real-inference
reproduction · final report (implemented/migrated/deployed/rejected/limits).
Checklist: P3.1, P3.2, P3.3, Quick Upload, Regression Matrix, Strict Ready,
Invariants.

---

## COVERAGE MAP (checklist section → checkpoint; machine-checked)

| Checklist section | CP |
|---|---|
| Non-Negotiable Invariants | CP12 (+ every CP exit gate) |
| P0.8 Schema Enforcement At Storage Boundaries | CP3 |
| Temporal RAG Program | CP9 (hooks) / CP11 (T-MAIN) |
| Phase 0 - Trustworthy Catalog | CP6 |
| Phase 1 - Deterministic `librarian_card.v0` | CP7 |
| Phase 2 - Deterministic Query And Seat Policy | CP7 |
| Phase 3 - Deterministic Hardening | CP5 |
| Phase 4 - Optional LLM Amplification | CP10 |
| P0.1 Stop Placeholder Summaries | CP2 |
| P0.2 Repair Degenerate Hierarchy | CP6 |
| P0.3 Finish Corpus-Floor Calibration | CP7 |
| P0.4 Make Answerability Honest | CP6 |
| P0.5 Complete Chunk And Metadata Hygiene | CP6 |
| P0.6 Corpus Deletion And Orphan Cleanup | CP6 |
| P1.1 Establish The Evaluation Set First | CP7 |
| P1.2 Activate The Grounded Planner Safely | CP10 |
| P1.3 Add Conversation And Open-Book Anchoring | CP11 |
| P1.4 Route Shelves Before Books | CP7 |
| P1.5 Implement Librarian Selection Roles | CP7 |
| P1.6 Route By Answer Shape | CP11 |
| P1.7 Batch And Cache Vocabulary Work | CP5 |
| P1.9 Qdrant Hot-Path And Contention Audit | CP6 |
| P1.9 Make Reranking Adaptive | CP11 |
| P2.1 Version The Universal Concept Contract | CP5 |
| P2.2 Build Two-Sided Multi-Point Concept Representations | CP10 |
| P2.2c Query-Side Retrieval Optimization | CP10 |
| P2.3 Add Versioned Qwen3 Retrieval Embedding Instructions | CP5 |
| P2.4 Negation And Relation Correctness | CP8 |
| P2.5 Typed Relation Signatures | CP8 |
| P2.5a Unified Claim/Assertion And Mechanism-Frame Contract | CP8 |
| P2.5b Canonical Semantic Artifact Envelope | CP3 |
| P2.5c Structured-Output Gateway | CP4 |
| P2.6 Engine Parity And Provenance | CP9 |
| P2.7 RunPod Production Validation | CP9 |
| P2.7b RunPod Burst Orchestration | CP9 |
| P2.8 Direct Concept-To-Document Grounding | CP9 |
| P3.1 Pilot Claim And Mechanism-Frame Routing | CP12 |
| P3.2 Deterministic Motif And Analogy Bridge Cards | CP12 |
| P3.3 Collection Consolidation - Migration | CP12 |
| P3.4 Quantization - Experiment | CP6 (owner-committed 40x) |
| P3.5 Reranker Serving Alternatives - Experiment | CP11 |
| Quick Upload And Filesystem Contract | CP12 |
| Required Three-Tier Regression Matrix | CP12 |
| Definition Of Strict Ready | CP12 |

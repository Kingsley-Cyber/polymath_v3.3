# BUILDLINE — the temporal north-star (authoritative build order)

**What this is:** the single time-ordered line of checkpoints from today's repo
state to Definition of Pass. The checklist holds item-level truth; the design
docs hold architectural truth; THIS file holds temporal truth — what is being
built NOW, what gates it, which documents it consumes, and what comes next.

**The standing rule that prevents the Qwen-instruction class of gap:** every
adopted design decision MUST claim a checkpoint slot the same day it is
adopted. A design with no slot is listed under UNSLOTTED at the top and is a
planning defect until placed. (The embedding-instruction gap survived weeks
because nothing enforced this; it is now CP5.)

**Maintenance:** senior updates the NOW pointer and stamps; executor stamps a
checkpoint's status as part of its receipts; owner vetoes via COORDINATION.
Precedence: OWNER > CONTINUATION_HANDOFF §Decision authority > this order >
day-to-day directives.

---

**NOW → CP1** (Phase A: g1 caught a REAL defect — digital-PDF structural-lane
bypass; fix CP1-D1 in flight, then full gate re-run)
**UNSLOTTED:** none (all adopted designs hold a slot as of 2026-07-14)

---

## CP0 — Foundation ✅ DONE 2026-07-14
S0 dark-code landing · S1 temporal summary seam · S2 bibliographic capture ·
S3 disposition matrix · 3-tier regression (negatives 5/5 all tiers; scorer v4)
· all 8 owner registries persisted + loader/resolver + hash taxonomy (31 tests)
· glide-path authority ratified. Receipts: checklist Implementation Log;
docs/baselines/EVAL_POSTS2_COMPARISON_RECEIPT_2026-07-14.md.

## CP1 — Deployment validation (Phase A) ⏳ ACTIVE (executor)
**2026-07-14 g1 FINDING:** digital PDFs bypass structure parsing
(_parse_pdf_fast_text) → flat tier_c/ocr_ast parents, empty heading_path.
CP1-D1 fix directive issued (COORDINATION #2). Also rewrites ecom evidence:
heading poverty = pipeline artifact → ecom exits CP2, gets
fix→reingest→single-enrichment path (owner §8 decision with corrected facts).
Consumes: docs/REBATCH_RUNBOOK_2026-07-14.md §Phase A.
Exit gate: g1–g10 green with fixture ground-truth assertions; senior verifies
receipts. Authority: senior AUTO. Failure = STOP + diagnose, no gate weakening.

## CP2 — Corpus enrichment (Phase B, MARK-ONLY per g1 finding) — next
Consumes: runbook §Phase B; latent_concept_policy.v1 (interim-v1 capture).
mark full regen → ecom clean-docs → lexicons → cards LAST → readiness →
after-eval vs today's baselines. Exit gate: coverage censuses + reconciliation
+ no-regression eval. Authority: 2h GLIDE after CP1 verification.
OWNER-ONLY carve-outs: ecom junk deletion, v2 unfreeze.

## CP3 — P2.5b envelope completion
Consumes: FINAL_SCHEMA §Shared envelope/§hash taxonomy/§Identifier recipes;
models/hash_taxonomy.py + registry_loader.py (built).
Remaining: identifier recipes + golden vectors · legacy adapters ·
projection outbox + ProjectionManifests (incl. embedding_profile w/
instruction_version) · Mongo validators · UGO annotate-only canary.
Exit gate: P2.5b acceptance boxes (byte-exact vectors, adapter parity,
manifest-reproducible identity sets, no live-behavior change). AUTO.

## CP4 — P2.5c structured-output gateway
Consumes: docs/STRUCTURED_OUTPUT_GATEWAY_SPEC_2026-07-14.md.
SemanticDigestV1 + capability ladder + semantic validator + targeted repair +
dead-letter + provenance/cache. Exit gate: P2.5c acceptance (UGO 10-packet
canary, ladder downgrade test, Ghost A untouched). AUTO.

## CP5 — S5 rollup + query-side activation round 1
Consumes: REBUTTAL restatement (alias absorption); registry-audit collisions
(3 alias stores → ONE registry; qdrant latent payload-whitelist fix; dual
entity_id reconcile); embedding_instruction_registry.v1 (APPROVED wording).
Exit gate: rollup censuses + the ISOLATED universal-instruction A/B
(vs baseline_live_v0; lay-language/cross-corpus up, no shape regression,
negatives 5/5). Authority: build AUTO; instruction flip GLIDE 4h.

## CP6 — Honesty layers
S6 readiness split (operational/metadata-quality/temporal inside
corpus_readiness) · S7 facet DF rule behind its A/B (GLIDE) · v2-naive +68k
reconciliation via ProjectionManifest audit. Exit gate: readiness recompute
receipts + facet A/B + reconcile explanation. 

## CP7 — Librarian measurement moment
S8: pair card rebuild FINAL (post-capture) + shelf_reserve A/B on frozen suite
(flip via GLIDE if gates pass) + watch-list re-check. Exit gate: preregistered
thresholds. Consumes: PLAN_CRITIQUE S8 row.

## CP8 — Claim spine proven on canary
Consumes: docs/LOCAL_EXTRACTION_THREE_SCHEMA_DESIGN_2026-07-14.md +
extraction_vocabularies.v1 + backend/evals/semantic_extraction_gold_v1.json.
LocalExtractionV1 + Python claim compiler on UGO; C2 GLiREL re-benchmark
(WITH vs WITHOUT controlled-label candidates on compiled-claim quality).
Exit gate: C2 verdict decides Stage-4 GLiREL; claims annotate-only. AUTO.

## CP9 — Semantic pipeline on the PoC pair (S11)
Consumes: FINAL_SCHEMA owner-rebuttal addendum (staged pipeline, permission
ladder) + all registries + motif matcher contract (sequence-tolerance +
role-threading recipes, MotifCandidate dual scores) + latent policy (claim-
grounded v2 corroborates interim-v1). Exit gate: assignment-state censuses,
observation-lane discipline (nothing self-promotes), no-regression eval. AUTO
build; any cutover GLIDE.

## CP10 — Retrieval activation family-by-family
Consumes: docs/RETRIEVAL_OPTIMIZATION_PLAN_2026-07-14.md + P2.2/P2.2c boxes.
Representation points (incl. context-enriched children) + mode→family matrix +
lineage dedupe + rank fusion; EACH family earns its seat via its own A/B.
Exit gate: the preregistered program target — lay-language/cross-corpus
doc-hit +10pts, no material regression. This is where the program's promise
is measured. GLIDE per family flip.

## CP11 — Time, anchors, and the model decision
S9 anchoring + P1.6 answer shapes · S12 T-MAIN temporal phases · target-stack
decision: 4B embedder + binary quantization as ONE migration (RunPod re-embed,
combined recall A/B) + reranker cascade A/B (may run earlier as its own slice).
OWNER decision on the migration budget.

## CP12 — Closure
S13 experiments (thematic pilot, bridge cards) · S14: full 16×3 regression
matrix, strict-ready verification, restart/rollback/concurrency test sweep,
deploy-from-main real-inference reproduction, final report distinguishing
implemented/migrated/deployed/rejected/external-limits. Definition of Pass.

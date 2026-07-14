# Re-Batch Runbook — test corpus tonight, full corpus enrichment after (2026-07-14)

ROLES: Claude = senior dev/planner (this spec). Codex = executor. Owner = gates.
Executor protocol: CONTINUATION_HANDOFF.md §Protocol applies (read-from-disk,
receipts with true exit codes, scratch checklist boxes per completion rule,
commit+push branch AND HEAD:main after each phase).

## What "re-batch" means tonight (and what it does NOT)

Tonight's re-batch = **enrichment passes over EXISTING chunks** (summaries with
the S1 capture contract, extraction with temporal wire-v3, deterministic
backfills, lexicon+card rebuilds LAST). It is NOT a from-source re-ingest:
the signed evidence (DISPOSITION_MATRIX_2026-07-13.md) shows full reingest
reproduces source-baked defects on ecom and is prohibited on frozen v2.
The claim/superframe/domain/motif layer is DESIGNED but UNBUILT (P2.5b gate)
— tonight captures interim-v1 latent candidates per
backend/registries/latent_concept_policy.v1.json; claim-grounded v2 corroborates
them later. No re-batch step below re-runs when the semantic layer lands —
capture-before-rebuild is respected.

## PHASE A — test-corpus deployment validation (run FIRST, ~30-60 min)

Objective: prove a FRESH corpus auto-produces every artifact end-to-end on the
current deployed stack before spending on the full re-batch.

A1. Create test corpus via the real API (owner supplies 3–10 mixed-format docs
    — at least one PDF, one .md, one transcript-like file):
    - POST /api/corpora (name: rebatch_smoke_YYYYMMDD) + document upload via
      the normal ingest path (frontend or API). NO hardcoded corpus IDs —
      discover from response.
A2. Wait for ingest completion (poll corpus readiness endpoint; ingest-worker
    logs via docker logs polymath_v33-ingest-worker-1).
A3. GATES — every one must pass, receipts logged with true exit codes:
    g1 chunks: parents+children exist; heading_path non-empty on structural
       parents; parent/child counts sane (children >= parents).
    g2 summaries: 100% summary-required parents have summary; latent_concepts
       present on >=30% of substantive parents (interim-v1 expectation);
       temporal_class present on 100% of summarized parents; aliases arrays
       populated where latent exists.
    g3 bibliographic: documents carry author/language/document_date/
       source_published_at where front-matter supports it; nulls carry reason
       codes (honest-null check).
    g4 extraction: ghost_b rows for 100% of eligible children;
       temporal_captures present where dates exist in text; engine recorded.
    g5 lexicon: corpus_lexicon entries > 0; canonical_key join works
       (spot-check 3 entities vector<->graph).
    g6 cards: librarian_cards = 1 per document; central_subjects non-empty.
    g7 projections: Qdrant naive/hrag/graph collections exist with
       points == expected (children [+ summary-eligible parents]); Neo4j
       entities present.
    g8 readiness: corpus readiness endpoint green (fully_enriched or
       explicitly-reasoned partial).
    g9 retrieval smoke: 3 questions via /api/chat SSE (one direct, one
       plain-language, one absent-topic) on all 3 tiers — direct+naive answer
       with citations from the test corpus; absent-topic fail-closes.
    g10 idempotency: re-trigger enrichment on the same corpus; row counts
       unchanged (no duplicates) — this is the restart/idempotency receipt.
A4. If ANY gate fails: STOP, diagnose, fix, re-run Phase A. Do not proceed.

## PHASE B — per-corpus re-batch (tonight, after Phase A green)

Order and disposition (matrix-aligned; each step backup-first, resumable,
true-exit-logged):

B1. markbuildsbrands (1,015 parents / 3,791 children) — full enrichment:
    a. S4 latent+temporal summary regen: backend/scripts/s4_quarantine_stale.py
       markbuilds all --apply (backup auto-written), then
       scripts/polymath_summary_backfill_scoped.py --corpus-id <full-cid>
       --apply in bounded batches; PIN pool to deepseek-v4-flash
       (owner directive "use flash"; current keys valid until rotation).
    b. Verify: latent coverage report (expect 60-90% of transcript parents;
       thin ad-chunks may honestly emit []); temporal_class 100%;
       p0_1_summary_integrity.py verify green; summary points reindexed
       (Mongo == Qdrant projections).
    c. Re-extraction: already complete (3,791/3,791 with temporal) — SKIP.
B2. ecommerce (10,222 parents / 56,996 children) — clean-docs enrichment:
    a. Projection heading-repair (free, from matrix §Group C recommendation)
       IF §8 signed for it; else skip heading repair, proceed with summaries.
    b. S4 summary regen on the 61 CLEAN + 6 SUSPECT docs (93.9% of parents);
       the 9 corrupt + 3 junk docs are EXCLUDED until owner signs §8
       (deletion + subset reingest are owner-gated).
    c. Re-extraction of clean docs' children via RunPod flash (temporal
       wire-v3, multi-account): plan_extraction_jobs include_succeeded=True
       scoped to clean doc_ids AFTER deleting their backed-up ghost_b rows
       (backup first — poc_reextract pattern). If RunPod quota/cost is a
       concern tonight, defer extraction to S11 and run summaries only.
    d. Verify: same gate set as B1b + reconciliation counts.
B3. UGO — SKIP (canary complete: 203/203 temporal, latent, bibliographic).
B4. polymath_v2 — FROZEN. Deterministic-only tonight:
    NOTHING paid. Optional if owner says "v2 deterministic ok": temporal
    classifier backfill (T-MAIN Phase 3 pattern) + doc_name/source-identity
    projection repair. Paid summary regen of 130k parents is OUT OF SCOPE
    until owner explicitly unfreezes with a budget.
B5. AFTER all capture passes land: lexicon rebuild per corpus
    (backfill_corpus_lexicon --apply) THEN card rebuild (build_librarian_cards)
    — cards exactly once, after all capture (standing rule). Then corpus
    readiness recompute.
B6. Final: 3-tier smoke on mark+ecom (6 questions), reconciliation script
    (backend/scripts/reconcile_qdrant_mongo.py), commit receipts to checklist
    Implementation Log, push.

## Cost/wall guardrails (tonight)

- mark S4: ~1,004 paid summary calls (deepseek-flash) — minutes-scale cost.
- ecom S4 clean-docs: ~9,000-9,600 paid summary calls — bounded batches of
  500 with per-batch verify; abort switch = stop between batches.
- ecom re-extract (optional tonight): 51k children via RunPod burst — use the
  P2.7 gate throughput (37.2/22.6 chunks/s) for wall estimate (~25-40 min);
  scale-to-zero after.
- Preflight canary EVERY paid stage: 3 rows first, verify fields, then batch
  (silent-fallback accounting rule).
- Per-provider empty-latent counter REQUIRED on S4 runs (Hy3 canary produced
  one latent=[] on substantive text — if any provider's empty rate >30% on
  substantive parents, drop it from the pool mid-run and log).

## Blockers that gate parts of tonight

- Owner §8 sign-off: required ONLY for ecom junk-doc deletion + subset
  reingest + (per wording) heading projection-repair. Summaries + extraction
  on clean docs proceed without it.
- Key rotation: NOT a blocker tonight (owner: rotation later this week; current
  keys work until then).
- v2 unfreeze: explicit owner phrase required for anything paid on v2.

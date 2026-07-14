# CODEX MISSION — one session, end-to-end completion (owner-issued 2026-07-14)

**You are the SOLE EXECUTOR of the entire remaining Polymath program.** The
senior (Claude) supervises, reviews, and rules through COORDINATION.md — the
senior never executes. The owner will NOT return to restart you: **this
session ending before completion IS program failure.** Your prime directive,
above any single task: SURVIVE TO COMPLETION.

## SURVIVAL PROTOCOL (overrides your instinct to wrap up)

1. **Never self-bound.** Your job is the ENTIRE BUILDLINE (CP2 → CP12), not
   any single queue item. Finishing a task means STARTING THE NEXT ONE, not
   closing the session. The ONLY terminal state: CP12's exit gate receipts
   complete and the final report committed.
2. **Disk is your memory.** After EVERY task: update PROGRESS.md (cursor:
   current CP, current task, next task, open threads), commit, push. Assume
   your conversation memory can vanish at any moment; everything needed to
   continue must live in the repo. If your context compacts, immediately
   re-read: PROGRESS.md → COORDINATION.md ACTIVE DIRECTIVES → this file →
   BUILDLINE.md → the current CP's consumed docs. Then continue from the
   cursor. Re-reading and continuing is ALWAYS the correct move after any
   confusion.
3. **Never exit on a blocker.** On any failure: diagnose with receipts, try
   ≥3 distinct remediations where safe, post a BLOCKER entry to
   COORDINATION.md — then PARK that task (record it in PROGRESS.md §Parked)
   and CONTINUE with the next non-dependent task. Poll COORDINATION.md for
   the senior's ruling between tasks and resume parked work when ruled.
4. **Senior review loop.** After each RECEIPT/BLOCKER/QUESTION entry, poll
   COORDINATION.md every ~2 minutes for up to 20 minutes for a senior entry.
   If none arrives AND the task is marked PRE-AUTHORIZED in the task list
   below, proceed; otherwise park it and continue other work — never idle,
   never exit. The senior's monitor is watching; rulings usually arrive fast.
5. **Owner-gated items** (marked OWNER below) may NEVER be executed on your
   own or the senior's authority. If unresolved when everything else is done,
   document them as external limits in the final report — that still counts
   as completion.
6. **Protect yourself from context bloat**: keep command outputs tailed,
   never cat huge files, use the true-exit pattern
   `sh -c 'cmd > /tmp/X.log 2>&1; echo EXIT=$? >> /tmp/X.log'` and read tails.

## GOAL-DRIVEN PRIORITIZATION (owner correction 2026-07-14 — binding)

**OWNER EMPHASIS (verbatim priority): fix SCHEMA → METADATA → RETRIEVAL CORE
first — only then fan out to the others and finish up.**

The core of this program: **lay-language question → right passages across
corpora → grounded answer.** Real progress = movement on TRACK A below.
Before starting ANY task ask: "does this move the core spine or its direct
prerequisite?" If not, it is TRACK B — executed ONLY while Track A is blocked
on a senior ruling, an owner gate, or a long-running unattended job. Track B
NEVER preempts Track A. Associative/hygiene work queued ahead of spine work
is the failure mode the owner explicitly corrected — do not recreate it.

**TRACK A — THE CORE SPINE (strict order):**
A0 = CP5/T5.6 universal-instruction A/B (query-side recall lever, zero
     schema dependency — run FIRST; requires a backend rebuild: allowed, no
     paid batch is running)
A1 = CP3-lean: T3.1 adapters + T3.2 writer acceptance (only what claims/
     digests need to exist; T3.3 outbox drain + T3.4 UGO canary slot in when
     A4 needs projection)
A2 = CP4 gateway complete (T4.1–T4.4)
A3 = CP8 claim spine on UGO + C2 verdict (T8.1–T8.5)
A4 = CP9 semantic pipeline + THE ONE PAID PASS on mark + projections
     (T9.1–T9.5; T3.3/T3.4 activate here)
A5 = CP5-core consumption: T5.2 latent payload whitelist, T5.1 alias
     registry + digest-era latent rollup, T5.3 entity_id reconcile
A6 = CP10 activation family-by-family + THE MEASUREMENT (T10.1–T10.6):
     lay-language/cross-corpus doc-hit +10pts is the program's promise —
     this is where it is proven or falsified.

**TRACK B — SUPPORT (fill-in only, never preempts A):**
CP6 hygiene (T6.1–T6.8, incl. quantization + Neo4j purge — purge is the ONE
B-item that hard-gates A's ecom lane at CP9), CP7 librarian A/B, CP5
remainder (T5.4/T5.5), CP11 (T11.1–T11.4), P0.1 verify boxes, after-eval
tooling. RULE: retrieval-behavior FLIPS are one-at-a-time — a Track B A/B
never overlaps a Track A measurement window.

**CP12 closure runs last, always, after A6.**

## STANDING RULES (from CONTINUATION_HANDOFF.md + checklist header — binding)

Read-before-act from disk · anti-gaming absolute (no eval-keyed logic, no
gate weakening, no denominator games, no fabricated receipts) · capture-
before-rebuild · one contract change = one pass · backups before destructive
ops · true exit codes · settings_service.attach(db) in standalone scripts ·
blue-green + synthetic canary for ANY RunPod endpoint deploy (in-place is
untrusted) · no container rebuild while a paid batch runs · keys stay in
encrypted settings · commit messages end "Co-Authored-By: Codex <executor>" ·
pull --rebase --autostash before commits · push branch AND HEAD:main ·
scratch checklist boxes only per completion rule with receipts.

## THE TASK LIST (execute in order; □ = your work; each with receipts)

### CP2 — SUPERSEDED BY OWNER SEQUENCING RULING (2026-07-14)
Owner ruling: NO paid enrichment before the semantic schema era — parent-level
semantics for mark (and ecom) are generated ONCE, claim-grounded, via the
gateway SemanticDigest at CP9. The interim-v1 mark regen (old T2.1–T2.5) is
CANCELLED — do not quarantine, do not regen, do not spend. Absorbed items:
□ T2a P0.1's four verify boxes → run READ-ONLY during CP6 (they verify
  existing summary integrity; no regeneration involved).
□ T2b The after-eval machinery (old T2.6) → build it during CP3 downtime
  (script + tests only; first real run happens after the CP5 instruction A/B).
Mark's 3 already-regenerated canary parents stay as-is (harmless, receipted).

### CP3 — Envelope + identity [P2.5b · P0.8]
□ T3.1 Legacy adapters (models/legacy_adapters.py + tests): adapt legacy
  documents/ghost_b rows/parent summaries/lexicon entries into contract-valid
  envelope equivalents; NEVER relabel observations as accepted; doc-id
  compatibility alias + needs_owner_lineage flag (see identifier_recipes).
□ T3.2 Mongo validators integration for envelope-era fields (extend
  apply_mongo_validators; warn-first) + typed-model acceptance at the summary
  writer boundary (P0.8 last box).
□ T3.3 Outbox drain worker: consume models/projection_outbox.py entries →
  Qdrant/Neo4j applies with retry/dead-letter; unit tests with fakes; wire
  BEHIND a flag (default off) — no live cutover.
□ T3.4 UGO annotate-only canary: run the envelope path end-to-end on UGO in
  annotate mode; acceptance: byte-exact goldens hold cross-process; legacy
  adapters produce valid equivalents; Fast/Hybrid/Graph behavior UNCHANGED
  (3-question smoke identical answers/citations). Scratch P2.5b acceptance
  boxes that literally pass. RECEIPT.

### CP4 — Structured-output gateway [P2.5c]
□ T4.1 models/semantic_digest.py: owner's SemanticDigestV1 VERBATIM from
  docs/STRUCTURED_OUTPUT_GATEWAY_SPEC_2026-07-14.md §3 + schema-hash golden.
□ T4.2 models/semantic_validator.py per spec §6 (every rule; location-indexed
  errors) + tests for each rule positive/negative.
□ T4.3 services/semantic_gateway.py: capability ladder (LiteLLM
  supports_response_schema → Tier1; Tier4 JSON-mode+validate+1retry fallback;
  Tier2/3 stubs raising NotImplemented with clear message), targeted-repair
  attempt-2, dead-letter queue collection, provenance record + cache key per
  spec §9. NO Ghost A changes (cutover stays gated).
□ T4.4 UGO 10-packet canary through Tier1 (packets built from UGO claims-era
  inputs = accepted local artifacts; interim: use parent text + extraction
  entities as the packet evidence): 0 structural failures, ≥1 exercised
  repair, 1 synthetic dead-letter demo, ladder downgrade test (Tier1 vs
  Tier4 schema-identical). Scratch P2.5c boxes. RECEIPT.

### CP5 — Vocabulary/alias/instruction layer [P1.7 · P2.1 · P2.3 · Lib Ph.3]
□ T5.1 (latent ROLLUP portion of S5 moves to CP9/CP10 — rolls digest-era
  latent, not interim-v1) ONE versioned alias registry absorbing the 3 existing stores (lexicon
  identity aliases, curated CONCEPT_ALIASES python, latent_concepts[].aliases)
  per REBUTTAL restatement; migration script backup-first; consumers repointed.
□ T5.2 Fix qdrant_writer summary payload whitelist to carry latent_concepts;
  reindex mark+UGO summary points; verify payloads.
□ T5.3 Reconcile dual entity_id builders (lexicon _entity_id vs graph
  entity_id_from_name) — one canonical fn, adapter for the other, tests on
  hyphen/underscore names (registry-audit collision receipt).
□ T5.4 P1.7 remainder: gather-before-fanout, query-vector reuse, per-corpus
  batching, lexical-only baseline mode + measurement.
□ T5.5 P2.1 lexicon contract fields (DF/specificity, senses, salience,
  provenance classes, slim payloads) additive migration.
□ T5.6 P2.3: universal instruction ISOLATED A/B (swap
  QWEN3_QUERY_INSTRUCTION to registry v1 universal per
  backend/registries/embedding_instruction_registry.v1.json; Fast tier first
  then all tiers vs baseline_live_v0). PRE-AUTHORIZED to flip if gates pass
  (lay-language/cross-corpus up, no shape regression, negatives 5/5);
  else revert and record. RECEIPT with numbers.

### CP6 — Repo-level hygiene + Qdrant optimization [P0.2 · P0.4 · P0.5 · P0.6 · Lib Ph.0 · P1.9 · P3.4]
□ T6.1 P0.6 reopened: batched Neo4j purge (fix the 716MiB OOM; bounded
  batches + transient retry), deletion-scheduled API honesty + status marker,
  in-flight job fencing at delete; unit tests; live drill on a throwaway
  fixture corpus (create → delete → zero-residue census incl. Neo4j).
□ T6.2 P0.6 remainder: lease test matrix (restart-before-expiry,
  purge-beyond-lease), scheduled reconciliation job.
□ T6.3 P0.2: five-format hierarchy fixture validation; singleton
  omit-vs-alias decision implemented (pick omit unless evidence demands
  alias — document); storage/round-trip measure.
□ T6.4 P0.5 remainder: doc_name/source-identity backfill (projection),
  curated-aliases → versioned data (feeds T5.1 if not already), facet
  diagnostics surface.
□ T6.5 P0.4: contradiction-check scoped design + asserting test (small,
  honest scope — detector flags contradictory atoms in evidence, surfaces in
  diagnostics only).
□ T6.6 P1.9 hot-path audit (all 17 boxes): per-stage timing incl. Qdrant
  server time separately, payload selectors, batching, filter+29-index audit,
  prevent_unoptimized, topology/image pin, priority classes, reranker
  admission, keepalive. Receipts per box.
□ T6.7 P3.4 QUANTIZATION (owner-committed 40x): clone-first on a copied
  collection, binary quantization + oversampling + full-vector rescore
  (already implemented, promotion-gated), recall A/B vs unquantized on the
  frozen suite; PRE-AUTHORIZED to promote per-collection if recall holds
  (doc-hit no material regression); else receipt and park.
□ T6.8 v2-naive +68k reconciliation via ProjectionManifest audit; additive
  manifest-driven rebuild ONLY if audit proves stale points (backup/report
  first). RECEIPT.

### CP7 — Librarian measurement moment [P0.3 · P1.1 · P1.4 · P1.5 · Lib Ph.1/2]
□ T7.1 Pair/mark card rebuild FINAL (post-capture) + Librarian Ph.1/2
  milestones (query→ID resolution wiring, tier-0 candidate gen, descent,
  bridge-seat evidence requirement).
□ T7.2 P1.4 shelf routing (corpus profile cards, kind-indexed, route-before-
  books, fan-out fallback).
□ T7.3 P1.1 remainder: librarian rubric; automated contamination check in
  eval runs; standing before/after gate wiring.
□ T7.4 shelf_reserve A/B on frozen suite. PRE-AUTHORIZED to flip
  SHELF_RESERVE_ENABLED if preregistered gates pass; else keep dark + receipt.
  P0.3 final acceptance measured here. RECEIPT with full numbers.

### CP8 — Claim spine on canary [P2.4 · P2.5 · P2.5a]
□ T8.1 LocalExtractionV1 models VERBATIM from
  docs/LOCAL_EXTRACTION_THREE_SCHEMA_DESIGN_2026-07-14.md + vocab registry
  literals; spaCy observation compiler integration (semantic_observations.py
  exists — extend to full LocalExtractionV1).
□ T8.2 Python claim compiler (ClaimRecordV1): merge spaCy structure + GLiNER
  spans + relations + qualifiers per the design's acceptance rules; multi-
  claim sentences + RESULTS_IN links; discourse-rules-first multi-sentence.
□ T8.3 P2.4 negation + P2.5 typed signatures inside the compiler contracts.
□ T8.4 Run on UGO (annotate-only, claims are candidates); censuses.
□ T8.5 C2 GLiREL re-benchmark: compiled-claim quality WITH vs WITHOUT
  controlled-label GLiREL candidates on
  backend/evals/semantic_extraction_gold_v1.json; verdict decides Stage-4
  GLiREL (relations stay observation-only unless WITH wins). RECEIPT.

### CP9 — Semantic pipeline + extraction on the pair [P2.6 · P2.7 · P2.7b · P2.8]
□ T9.1 Deterministic domain resolver (concept→domain registry over
  backend/registries/domain_registry.v1.json + affinity priors SERVE-ONLY) +
  superframe rule registry (relation→MF per design; versioned recipe data).
□ T9.2 Frame instances w/ role_bindings from compiled claims; motif matcher
  (sequence-tolerance + role-threading as versioned recipes; MotifCandidate
  dual scores; approved stage→superframe bindings registry).
□ T9.3 THE ONE PAID PARENT-SEMANTICS PASS: gateway-driven SemanticDigest over
  ALL mark parents (claim-grounded; summary + latent + domains + frames +
  motifs in ONE call per packet; permission ladder enforced; flash primary;
  durable jobs; canary-first). This replaces the cancelled CP2 regen — mark's
  parent semantics are bought exactly once, here, on the final schema.
□ T9.4 P2.6 engine parity contract + P2.7 remainder (5,000-chunk gate,
  parity compare harness, retry safety, readiness wiring) + P2.7b burst
  (chunk-complete barrier, manifest, per-burst metrics).
□ T9.5 P2.8 concept→doc grounding (core-lane provenance routing, anchor
  protection, polluted-card detection).
□ T9.6 [OWNER] ecom lane: reingest + junk deletion ONLY if owner lines exist
  in COORDINATION (see §OWNER DECISIONS below); otherwise execute everything
  ecom-independent and document ecom as owner-pending. RECEIPT.

### CP10 — Retrieval activation family-by-family [P2.2 · P2.2c · P2.2d · P1.2 · Lib Ph.4]
□ T10.1 Representation points (context-enriched child et al.) per P2.2 with
  attested/generated loops; projection via manifests + outbox.
□ T10.2 P2.2c query-side: per-family instructed query embeds (universal v1),
  mode→family matrix as policy data, lineage dedupe, rank fusion, rerank
  source-text-only; per-stage budgets + diagnostics.
□ T10.3 P1.2 grounded planner activation per its checklist safeguards.
□ T10.4 Librarian Ph.4 LLM amplification (planner-when-thin,
  explain-not-select).
□ T10.5 P2.2d forced latent sweep COMPARISON A/B (domain-conditioned sweep
  vs deterministic bridges; per checklist P2.2d acceptance).
□ T10.6 EACH family/behavior flips only on its own A/B pass (PRE-AUTHORIZED
  per-family flip on pass; park on fail). The program target measured here:
  lay-language/cross-corpus doc-hit +10pts, no material regression. RECEIPT.

### CP11 — Time, anchors, shapes, rerank serving [P1.3 · P1.6 · T-MAIN · P1.9b · P3.5]
□ T11.1 P1.3 conversation/open-book anchoring (anchors as priors, inspectable,
  follow-up tests).
□ T11.2 P1.6 answer-shape routing (shape policies, scaffolds,
  hydrate-before-cite, sibling expansion, dynamic K).
□ T11.3 T-MAIN temporal phases 2–7 (assertions/episodes/outbox projection,
  versioned RELATES_TO co-scheduled with P2.5 edges, query modes, ONE
  eligibility service, shadow-then-enforce).
□ T11.4 P1.9 adaptive rerank (latency-by-shape budgets, excerpts, cascade
  eval) + P3.5 serving alternatives EXPERIMENT (receipts; no model change
  without owner).

### CP12 — Experiments + closure [P3.1 · P3.2 · P3.3 · Quick Upload · Matrix · Strict Ready · Invariants]
□ T12.1 P3.1 thematic routing pilot (mark) + P3.2 bridge cards — experiments
  with before/after; adopt or reject WITH EVIDENCE.
□ T12.2 P3.3 collection consolidation — clone-first migration experiment;
  adopt/reject with evidence.
□ T12.3 Quick Upload decision boxes (watch-mode; inbox/dead-letter if adopted).
□ T12.4 Full 16×3 regression matrix with the complete record contract.
□ T12.5 Strict-ready verification per corpus; all reconciliations (Qdrant
  counts, Mongo ownership, graph promotion, source bindings, readiness
  denominators).
□ T12.6 Restart/resume/idempotency/rollback/concurrent-ingest test sweep.
□ T12.7 Final image rebuild from main + deploy + real-inference reproduction
  of the suite. Non-negotiable invariants final sweep.
□ T12.8 FINAL REPORT (implemented / migrated / deployed / rejected-with-
  evidence / external-limits incl. any unresolved OWNER items) + checklist
  fully scratched per completion rule + PROGRESS.md marked COMPLETE.
  This is the ONLY point you may end the session.

## OWNER DECISIONS (execute only if these exact lines appear in COORDINATION.md as `OWNER ::` entries or are relayed by the senior citing the owner)
- "ecom reingest approved" → T9.6 reingest lane.
- "junk deletion approved" → the 3 Group-D doc deletions (backup-first).
- "unfreeze v2 <budget>" → v2 paid passes; otherwise v2 = deterministic-only.
- Key rotation notice → pause between batches, senior re-verifies canaries.

## REVIEW CONTRACT
Every RECEIPT gets senior review via COORDINATION.md. PRE-AUTHORIZED items
proceed after the 20-minute poll window if no senior objection. Everything
else waits-and-parks, never blocks the session. You never edit senior text;
you never weaken a gate; when in doubt, post a QUESTION and keep working
elsewhere. Complete the mission.

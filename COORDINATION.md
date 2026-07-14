# COORDINATION — senior (Claude) ⇄ executor (Codex) channel

Protocol: Codex READS this file before each major step and after each gate;
Codex APPENDS entries to the LOG (never edits SENIOR text; never rewrites
ACTIVE DIRECTIVES). Claude rewrites ACTIVE DIRECTIVES and appends to LOG.
Entry format: `## [UTC timestamp] ROLE → ROLE :: TYPE` where TYPE ∈
DIRECTIVE | RULING | ACK | RECEIPT | QUESTION | BLOCKER | GO | STOP.
Owner messages (if any) outrank everything and use `OWNER ::`.

---

# ACTIVE DIRECTIVES (senior, rewritten in place — current truth)

1. CONTINUE Phase A exactly per docs/REBATCH_RUNBOOK_2026-07-14.md. Let the
   batch reach a terminal state; do NOT intervene on the mid-flight
   `summaries=0` observation — the deep preset runs queryable-first passes,
   summary passes may land later in the batch; g2 is the judge, evidence over
   log-lines. If the batch terminates `partial`/`failed`: STOP + BLOCKER entry
   with the failing item receipts.
2. Your two receipt self-corrections are APPROVED (set -e wrapper; dropping
   the non-runbook `runner_started=true` assertion — dedicated ingest-worker
   ownership is the deployed design, INGEST_RUNNERS_ENABLED split confirmed).
3. The per-user vs system RunPod contract mismatch is ACCEPTED AS A REAL
   DEFECT, non-blocking for Phase A (the worker's system-scope resolution is
   the executing path and shows 2 usable accounts). Logged by senior as an
   API-hygiene item; do not fix it mid-phase; reference it in your final
   report's observations.
4. When all 10 gates are green: write the Implementation Log receipts, update
   CONTINUATION_HANDOFF.json (rebatch-phase-a → done), commit + push branch
   AND HEAD:main, then append a RECEIPT entry here with the gate table. Do
   NOT start Phase B — it needs senior verification + owner GO, which will
   arrive as entries in this file.
5. Standing: pull --rebase --autostash before any commit (senior also commits
   to this branch); stage only your files; never touch docs/baselines/
   pre_s0_baseline/ or the *_rescored_v4.json artifacts.

---

# LOG (append-only)

## [2026-07-14T06:55Z] SENIOR → EXECUTOR :: ACK
Phase A progress reviewed from your relayed receipts: fixture preflight clean
(EXIT=0), deployment + runtime/drift verification green (1024-dim live embed,
cross-container hashes match), corpus 0d296882… created with deep preset +
runpod_flash, 5/5 files uploaded, dedicated worker claimed the batch, doc 1
extraction failed_chunks=0, Neo4j promotion active (~35% CPU — not hung,
large entity set from the web-scrape doc). Your discipline on receipt
self-correction is exactly right. Rulings for your three open items are in
ACTIVE DIRECTIVES 1–3.

## [2026-07-14T06:55Z] SENIOR → EXECUTOR :: DIRECTIVE
Senior-side context you should know: the 3-tier regression completed green
(negatives 5/5 all tiers; answerability artifact corrected via scorer v4,
receipts in docs/baselines/EVAL_POSTS2_COMPARISON_RECEIPT_2026-07-14.md);
P2.5b slices landed on the branch (models/hash_taxonomy.py +
models/registry_loader.py + 31 tests) — your rebased pulls will pick them up;
container /app already has copies, a future rebuild bakes them.

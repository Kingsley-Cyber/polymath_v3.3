# CONTINUATION HANDOFF — model-agnostic state of record

Purpose: ANY agent (Claude, Codex, or other) resumes the Polymath program from
this file with zero chat-history dependence. Updated 2026-07-14.

## Protocol (mandatory, in order)

1. Verify state: `git log --oneline -5`, `git status --porcelain`, branch
   `claude-continuation-20260713` (kept in sync with `main` via
   `git push origin HEAD:main`).
2. Read FROM DISK, fully, before any edit (never from your own memory of them):
   - `CLAUDE.md` (north-star rules: read-before-act, adopt-then-execute,
     rebuild freeze, receipts; applies to every agent, not just Claude)
   - `AGENTS.md` (Goal Drift + Qdrant references)
   - `docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md` (THE item-level ledger)
   - `docs/PLAN_CRITIQUE_2026-07-13.md` (S0–S14 sequencing + addenda)
   - `docs/FINAL_SCHEMA_METADATA_ARCHITECTURE_2026-07-13.md` incl. the
     "Owner rebuttal integration (2026-07-14, APPROVED)" addendum — wins over
     any conflicting earlier text
   - `docs/REBUTTAL_INTEGRATION_RESTATEMENT_2026-07-13.md` (owner rulings C1–C4)
   - `docs/SEMANTIC_EXTRACTION_PRODUCTION_READINESS_2026-07-13.md` (benchmarks)
   - `docs/EXECUTION_PLAN_2026-07-13.md` (533-claim auditable ledger)
3. Restate your interpretation + conflicts BEFORE editing anything semantic.
4. Execute jobs in dependency order (`CONTINUATION_HANDOFF.json`), attach
   receipts (commands + outputs, non-zero exit on failure) to every completion,
   scratch checklist boxes only per its completion rule.

## Non-negotiable invariants (compressed)

- Anti-gaming: no logic keyed to eval queries/corpora/concepts; no threshold
  weakening; no denominator redefinition; LLMs never pick final seats; no
  silent fallbacks — count and surface every one.
- Capture-before-rebuild; one contract change = one data pass; no dark fields
  (every captured field ships with a named consumer or is marked dark).
- Mongo authoritative; Qdrant/Neo4j rebuildable projections; original query =
  protected lane; corpus isolation; provenance everywhere.
- Deterministic Python owns accepted semantics; spaCy/GLiNER/GLiREL/LLM produce
  candidates; permission ladder: normalize → retain candidates → differentiated
  permissions (ground/recall/expand/explain) → corroborate → promote.
- polymath_v2 FROZEN for heavy ops. PoC pair = markbuildsbrands_transcripts +
  ecommerce_AI_FILM_SCHOOL. UGO = canary.
- `docs/` is gitignored: force-add (`git add -f`) any docs artifact meant to
  persist. Registries under `backend/registries/` are normally tracked.

## Ops gotchas (cost hours if ignored)

- Deploy: `docker compose -f docker-compose.yml -f docker-compose.override.yml
  -f docker-compose.offline-ingest.yml --profile offline-ingest up -d --build
  backend ingest-worker`; then `bash scripts/verify_backend_runtime.sh` (must
  exit 0). NEVER a lone `-f docker-compose.yml` (drops override → dead sidecars
  → silent vector=0).
- Two containers share the image; verify changes in BOTH backend and
  ingest-worker. Pipe exit codes lie: `sh -c 'cmd > /tmp/x.log 2>&1; echo
  EXIT=$? >> /tmp/x.log'`. Container scripts need `PYTHONPATH=/app` and
  `-w /app`.
- In-container tests: `mkdir -p /app/tests` + docker cp (tests are not baked
  into the image).
- Provider keys live encrypted in Mongo `settings.api_keys` (deepseek, longcat,
  runpod, runpod_accounts) — never in code or commits. Keys pasted in chat on
  2026-07-13/14 are due ROTATION by owner.

## Registries (owner-authoritative data, delivered 2026-07-14)

`backend/registries/domain_registry.v1.json` (D01–D16 + members),
`superframe_registry.v1.json` (MF01–MF16),
`domain_superframe_affinity.v1.json` (priors; may never force/forbid an
assignment). `motif_registry.v1.json` (M01–M12) — ALL FOUR delivered. Stage→superframe
bindings are not owner-delivered: resolvers must not use any such mapping
until the owner confirms one. Do not invent registry entries.

## State snapshot (2026-07-14)

DONE + verified (receipts in checklist Implementation Log + docs/baselines/):
S0 (dark code live: P1.7 cache, P1.8 warmup, shelf_reserve dark), S1 (temporal
summary seam; UGO 203/203 temporal_class), S2 (bibliographic capture+backfill;
681/681 stamped, honest nulls), S3 (disposition matrix built — §8 OWNER
SIGN-OFF STILL EMPTY), S4 canary (3 mark parents regenerated with
latent+temporal via new contract), 533-claim EXECUTION_PLAN (14/14 critiqued,
parity attested), Mongo↔Qdrant reconciliation receipt (UGO exact; pair
consistent; **v2 naive +68,785 unexplained points = OPEN**), semantic
benchmarks (spaCy 0.919 qualifier F1; GLiREL 0.174; Relex 0.098 → relations
observation-only pending C2 re-benchmark).

IN FLIGHT: post-change 3-tier held-out regression (`/tmp/post_s2_eval.log`,
renames to `docs/baselines/EVAL_2026-07-13_postS2_<tier>.json`; pre-change
baselines in `docs/baselines/pre_s0_baseline/`). Compare against preregistered
thresholds: naive+cross-corpus doc-hit +10pts goal for the program, no shape
regressing >5pts, negatives 5/5, latency within +20%.

BLOCKED ON OWNER: (1) key rotation → then S4 mark batch (1,004 parents;
driver `/tmp/s4_quarantine_stale.py` in backend container + 
`scripts/polymath_summary_backfill_scoped.py --corpus-id 5a20bc21-… --apply`;
pin pool to deepseek-v4-flash); (2) S3 matrix §8 sign-off; (3) S7 facet DF rule ("facet DF rule approved").

NEXT UNBLOCKED WORK, in order: P2.5b registry-independent slices (canonical
JSON serializer + hash-namespace golden vectors + identity recipes + legacy
adapters; UGO annotate-only canary), GLiREL C2 re-benchmark harness, S5
alias/latent rollup (absorb the 3 existing alias stores; fix Qdrant
latent_concepts payload-whitelist drop), S6 readiness split, S7 (after owner
phrase), S8 pair card rebuild FINAL + shelf_reserve A/B, S9–S14 per
PLAN_CRITIQUE.

## Job queue

Machine-readable: `CONTINUATION_HANDOFF.json` (same directory). Human order of
execution = PLAN_CRITIQUE S-map + checklist boxes. When you finish a job:
update the checklist with dated receipts, update CONTINUATION_HANDOFF.json
status, commit (`Co-Authored-By` your model), push branch AND `HEAD:main`.

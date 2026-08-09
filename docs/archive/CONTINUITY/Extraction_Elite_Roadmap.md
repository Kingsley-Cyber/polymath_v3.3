# ELITE LOCAL RAG — Master Execution Plan (2026-07-29)

Owner directive: primary extraction = **GLiNER + spaCy + Python. GLiREL retired.
Fast, deterministic, gated, covers all text.** This file is the complete
self-contained path from current state to done. Run the prompts in order; each
phase gates the next. Companion anchors: checklist **P2.10**, RTX spec **P2.9**
(`CONTINUITY/RTX_DEFAULT_EXTRACTION_CONTROL_PLANE.md`), COORDINATION log
2026-07-29 entries.

## DEFINITION OF DONE

| Dimension | Target | Proof |
|---|---|---|
| Relation precision | >= 0.80 on `spacy_relation_gate_v1` (frozen, sha256 1b92245b…) | gate receipt |
| Relation recall | >= 0.40 asserted edges; F1 > GLiREL (0.231) on same gate | gate receipt |
| Speed | ~130 ms/chunk M1 (GLiNER 110 + spaCy ~2-12 + rules 10); GLiREL's 210 ms deleted | measured, varied chunks |
| Scale | 100×1MB: extract ~4-5 h M1, or <1 h via RTX S0; queryable-as-it-goes | P5 receipts |
| Integrity | byte-identical reruns; zero invalid predicates; zero disallowed pairs; graph edges = asserted+positive only | tests + counters |
| Live quality | 20-edge hand spot-check >= 0.80 on real batch | P5 step 2 |
| Summaries | cloud (locked), `defer_summaries` for batch | unchanged |

## CURRENT STATE (verified 2026-07-29)

- Gate infrastructure DONE: fixture 11 samples / 15 asserted relations / 12
  predicate types (sha256 8d2912e2…); gate preregistered; both engines measured.
- Baseline: dep-path **P 1.000 / R 0.467 / F1 0.636** (gate PASS) vs GLiREL
  **0.273 / 0.200 / 0.231** (host MPS :8084). Gate GREEN 2026-07-29.
- FP remediation: 3 structural guards (single-clause, conjunct-crossing,
  exception-boundary) + 2 YAML __DROP__ rules (prep:for). 16 FP -> 0 in 2 iterations.
- Default = `glirel` (both `.env` and code fallback). Blast radius zero.
- Ontology ratified (owner 2026-07-29): (Method,Method)->uses,
  (Person,Concept)->uses, (Software,Software)->depends_on.
- `_VALID_PREDICATES` == 30-value Predicate Literal, exact, fail-loud at load.

## PHASE MAP

| Phase | What | Exit gate | Status |
|---|---|---|---|
| P1 | Gate infrastructure | fixture >= floor, preregistered, baselined | **DONE** |
| P2 | Precision remediation | gate GREEN (P>=0.80, R>=0.40) | **DONE** (P=1.000, R=0.467, F1=0.636) |
| P3 | Integration completeness | one parse/chunk, modules wired-or-deleted, contract clean | **DONE** (27x speedup, 0 drops, byte-identical) |
| P4 | Atomic flip | flip+gate one commit; 1-file canary clean; rollback proven | **LIVE IN CODE** (status corrected 2026-07-30 — see note) |
| P5 | Scale → production | 10-file batch clean; 20-edge spot-check >=0.80; 100-doc run | superseded by the RECALL LADDER (see below) |
| P6 | Speed ladder | GLiNER fp16/ONNX; RTX S0 for bursts | optional, after the ladder |

### STATUS CORRECTION 2026-07-30 (Step 0c of the recall ladder)

This table said P4 was `queued` while the flip had already shipped:
`ghost_b_local.py` pins the relation engine to spaCy and Stage C has run
dep-path in production since. The table was stale against the tree.

**The P4 "rollback proven" exit criterion was never satisfiable.** The documented
rollback — *"set `GHOST_B_RELATION_ENGINE=glirel`, recreate, confirm legacy
works"* — could not fire: the env var was read into a comment and ignored
(`_rel_engine = "spacy"  # was: os.environ.get(...)`), so setting it produced a
silent no-op. Step 0b resolved this by **deleting the rollback claim rather than
restoring the switch**: GLiREL is retired by owner decision, so the honest
posture is no runtime rollback at all. The var now raises a loud RuntimeError if
set to anything but `spacy`, and the unreachable GLiREL branch (plus a 1.87 GB
eager model load that ran on every extraction call) was deleted.
**Reverting the deterministic lane means reverting the commit, not flipping a
flag.** Proof: `backend/tests/test_relation_engine_switch_honesty.py`.

### SUPERSEDED BY THE RECALL LADDER (2026-07-30)

P5 as written measured scale, not yield. Live measurement since showed the
deterministic lane emits **0.04 relations/chunk** against GLiREL's 1.10 raw — a
precision win that bought near-silence. The successor plan is
**`CONTINUITY/DETERMINISTIC_RELATION_RECALL_SPEC.md`** (R-pre → R0 → R1–R8).
P5's 10-file batch and 20-edge spot-check survive inside it as acceptance steps;
the phase itself no longer runs standalone.

---

## SESSION HEADER — prepend to every prompt

```
CONTEXT — read before working:
  CLAUDE.md
  CONTINUITY/local_ingestion_phase_a/00_LOCKED_DECISIONS.md
  CONTINUITY/local_ingestion_phase_a/04_PRIOR_EXPERIMENTS.md
  docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md — P2.10
HARD CONSTRAINTS: Ghost B local+deterministic, NO LLM/SLM in extraction (locked).
ExtractionResult / relations[] shape unchanged. Byte-identical reruns.
ontology.yaml only when owner-ratified in the prompt. Default stays glirel
until P4. Gate spec + fixture immutable (new version if change is ever needed).
REPORTING: paste real output; MEASURED/PROJECTED labels with command/host/device;
NOT DONE / NOT RUN where true; a worse number is reported as worse.
```

## PROMPT P2 — PRECISION REMEDIATION (run now)

```
TASK: Drive precision 0.304 -> >=0.80 against the frozen gate. Taxonomy shows a
structural failure; ONE class of code change is authorized plus YAML.

0. OWNER-RATIFIED ONTOLOGY EDITS (comment "owner-ratified 2026-07-29"):
   (Method,Method)->uses · (Person,Concept)->uses · (Software,Software)->depends_on.
   Nothing else touches ontology.yaml.
1. STRUCTURAL GUARDS — code in dep_path_extractor.py, tests each:
   a) SINGLE-CLAUSE CONSTRAINT (12/16 FPs): reject any candidate whose signature
      has >1 predicate node (VERB/ROOT). FIRST print the 7 TP signatures and
      confirm none violates it; if one does, STOP and report.
   b) CONJUNCT-CROSSING GUARD: reject paths crossing cc into another conjunct.
   c) EXCEPTION BOUNDARY: path traversing prep:except/pcomp -> DROP.
   No other code changes.
2. YAML EDITS from taxonomy: prep:for -> __DROP__ for uses/depends_on object
   binding; type-constrain depend/develop/compose/define; fix uses-vs-depends_on
   precedence (direct object beats prep:on within one clause).
3. LOOP: apply 0+1, rerun gate, re-bucket; then YAML batches: edit -> rerun ->
   re-bucket, logging P/R/F1 each. MAX 5 iterations, then stop + residual
   taxonomy for owner.
4. POS ERRORS (stores/references tagged NOUN, 4 FNs): sm-vs-md A/B on THIS
   fixture. REPORT ONLY — model switch is an owner decision.
REPORT: TP audit, iteration log, final P/R/F1 both denominators, gate verdict,
md A/B table.
```

## PROMPT P3 — INTEGRATION COMPLETENESS (after gate GREEN)

```
1. ONE PARSE PER CHUNK: share one nlp + Doc across Stage B (appos) and Stage C
   (dep-path); nlp.pipe(batch_size=...); disable unused components (spaCy ner —
   GLiNER owns entities). Tests + gate must stay green; report before/after
   ms/chunk.
2. DEAD MODULES — decide each in one line: alias_resolver / coreference_heuristic
   wire with purpose+tests or delete; triple_verbalizer must NOT write Qdrant
   (needs its own CP10/P2.2 anchor) — delete or comment why unwired.
3. HONEST BENCHMARK: varied real chunks, warm: spaCy Stage C real runtime vs
   GLiREL host MPS :8084. ms/chunk both + projected per-doc at 188 tok/chunk.
4. CONTRACT: extract -> relations[] -> _validated_relations -> Pydantic ->
   JSONL -> backend parse; zero drops on gate fixture; qualifiers reach claim
   path; only is_graph_edge reaches the writer.
5. DETERMINISM AT SCALE: two runs over 100 real chunks, byte-identical.
```

## PROMPT P4 — ATOMIC FLIP (SHIPPED; step 1 corrected 2026-07-30)

```
1. [SHIPPED, with a correction] ONE COMMIT: code pinned to "spacy" + the passing
   gate receipt. The original step said "rollback documented (=glirel + restart)"
   — THAT ROLLBACK NEVER WORKED (env var was ignored; silent no-op). Step 0b of
   the recall ladder deleted the claim instead of restoring the switch, because
   GLiREL is owner-retired. There is no runtime rollback; revert the commit.
2. REBUILD (files were only docker cp'd — nothing is live until):
   docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml up -d --build backend
   (NEVER plain -f docker-compose.yml alone — drops the override, kills sidecars.)
   Then: bash scripts/verify_backend_runtime.sh
3. ONE-FILE CANARY (repo law): scratch corpus, one pilot file. Verify:
   failed_chunks=0; zero Pydantic drops; RELATES_TO count + predicate
   distribution vs a GLiREL run of the SAME file (flag <60% typed = floor,
   100% = junk); qualified_*/rejection counters populated; re-ingest ->
   byte-identical.
4. ROLLBACK TEST: =glirel, recreate, confirm legacy works; set back.
REPORT: commit hash, rebuild+verify output, canary receipts, both
distributions, rollback proof.
```

## PROMPT P5 — SCALE → PRODUCTION (after canary clean)

```
1. 10-FILE CROSS-DOMAIN BATCH (books + >=1 REAL transcript from the video
   corpus), defer_summaries on. Report: per-doc typed share (flag <60% / =100%),
   counter totals, low_parse_confidence fraction, sustained ms/chunk +
   chunks/sec, Qdrant/Neo4j verify green.
2. EDGE SPOT-CHECK: 20 random new RELATES_TO edges vs source sentences.
   Precision >= 0.80 required. Below -> STOP, new taxonomy, do not proceed.
3. 100-DOC RUN: extract-first (defer_summaries: true), embed/index after,
   summaries backfill via cloud lane. Report wall time per phase, chunks/sec,
   peak memory, RELATES_TO growth + distribution, flagged docs.
4. CLOSE OUT: scratch off P2.10 boxes with receipts; list anything open.
```

### POST-FLIP BACKLOG (recorded P4 senior review — do NOT implement in P5)

| ID | Item | Gate / Risk |
|---|---|---|
| PF-1 | Alias RESOLUTION at Neo4j writer boundary (canonical-node merge, anti-fragmentation) | Fuzzy 0.92 threshold; gated against false merges |
| PF-2 | AttributeRuler POS-override lexicon for domain verbs (stores/references/indexes) | sm and md make opposite POS errors; needs A/B per model |
| PF-3 | Copular-defines signature rule ("X is a Y" → defines) | FP risk — gated, needs fixture expansion first |
| PF-4 | Triple verbalization as a retrieval family | Needs CP10/P2.2 anchor; separate design doc |

## P6 — SPEED LADDER (optional, in order of payoff)

1. **GLiNER fp16/bf16 or the scaffolded ONNX lane** (`GHOST_B_GLINER_ONNX=1`) —
   GLiNER is ~85% of post-flip cost; gate with `onnx_equivalence_check.py`
   (ent Jaccard >=0.95, facet >=0.95). PROJECTED ~2x on the dominant stage.
2. **RTX S0** — zero code, per `RTX_SIDECAR_RUNBOOK.md`: launch sidecar with
   GHOST_B_GLINER_BATCH=128 GHOST_B_GLIREL_BATCH=256 GHOST_B_VRAM_TRIM_GB=4,
   point Mac via LOCAL_GHOST_B_EXTRACT_URL, 1-file canary. 10-30x for bursts.
   Full control plane = P2.9 spec (only if wake-on-demand is ever needed).
3. **RunPod extract-first** — already-ratified doctrine for massive jobs.

## ALL-TEXT COVERAGE

| Text type | Handling | Status |
|---|---|---|
| Books/papers/prose | core lane (GLiNER + dep-path + rules) | P2-P5 |
| Transcripts (ASR) | low_parse_confidence guard skips relation extraction on unpunctuated chunks (entities still run); real-corpus fixture sample required in P5 | guard built; validate P5 |
| PDFs incl. scans | docling structural lane (CP1-D1); OCR only for image-only | done |
| Tables | dedicated table facts (TABLE_MAX_FACTS_PER_CHUNK=24) | done |
| Code chunks | skipped by policy (correct — never extract) | done |
| Headings/fragments | verbless-root skip | done |
| Non-English | out of scope for en_core_web_sm — flag, don't parse; revisit only if corpus goes multilingual | accepted gap |
| Pronouns/coref | 3,026-claim backlog; coreference_heuristic wire-or-delete decision in P3 | open |
| Implicit/no-cue relations | known ceiling of syntactic extraction — semantics live in vectors/reranker/summaries (ratified trade, C2) | accepted |

## ELITE RETRIEVAL TRACK (after extraction lands — from RAG_SPEED_QUALITY_ROADMAP)

Priority order, all pre-analyzed in the tracker:
1. **C3** metadata-at-retrieval (highest leverage, NO re-ingest): answer-type/
   heading boosts in ranking_policy; decide facet fate in Qdrant.
2. **B2** query-guided parent excerpt; then **B3+B4** answerability QA pass
   (build once, gate + reorder).
3. **OPT2** domain routing (speed + precision, tags already exist).
4. Clean RELATES_TO graph (this plan's output) upgrades the Graph strategy.
5. **A8 ColBERT/PLAID** — highest ceiling, ONLY after P3.4 quantization; token-
   level vectors multiply storage ~100x, must not collide with the 40x memory win.

## STANDING LAWS (non-negotiable)

- Atomic flip law: flip + passing gate = one commit. Never a rebuild side effect.
- 1-file canary before any multi-file batch; new lane needs its own canary.
- Gate spec + fixture immutable after first decisive inference; changes = new version.
- No LLM/SLM in Ghost B extraction (locked; hallucinated values corrupt Neo4j).
- Ontology changes are owner-ratified only. Schema is authority; extractor conforms.
- Graph edges = asserted + positive only; everything else is a qualified claim.
- Evidence protocol: receipts or NOT RUN; MEASURED vs PROJECTED; worse = say worse.
- Monitors: typed share <60% = predicate floor, =100% = junk entities;
  20-edge spot-check on every new corpus type.

## OWNER DECISION REGISTER (only these need a human)

1. spaCy model switch (sm -> md/trf) if the A/B justifies — reproducibility impact.
2. New ontology predicates/pairs surfaced by taxonomy.
3. P2 plateau below 0.80 after 5 iterations — accept lower bar vs more code.
4. ColBERT adoption (A8) after P3.4.
5. Reopening local summaries (currently locked cloud) — only if privacy ever wins.

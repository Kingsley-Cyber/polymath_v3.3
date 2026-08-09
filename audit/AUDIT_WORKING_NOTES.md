# Forensic Audit — Working Notes (confirmed facts only)

Auditor session: 2026-08-06 ~22:50 onward. Repo: /Users/king/polymath_v3.3 @ commit 8614922 + dirty tree.
Pack under audit: POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK (entirely untracked in git — no commit history; evidence = mtimes + artifacts).

## Confirmed timeline backbone (all 2026-08-06, local UTC-6)

| Time | Event | Evidence |
|---|---|---|
| 13:08 | GRAPHIFY_GLINER2_CPU_AGENT pack delivered (predecessor lane, entity census refactor) | dir mtimes; artifacts through 16:56 |
| 16:52 | Exposed benchmark created: ~/Downloads/technical_book_graphrag_stress_test (fixture + 66-positive/5-qualified gold key) | dir mtimes |
| 17:15 | POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK delivered | file mtimes |
| 17:23–18:02 | Implementation stages executed (CAPTURE_BASELINE → WIRE_ASSERTION_GATE receipts) | work/reports/*.md mtimes |
| 18:49 | First full stress score vs 66-key: **35/66 matched (recall 0.53)**, 26 unmatched promoted, gate positive_recall>=0.80 FALSE | work/stress_test/score.json mtime 18:49:53 |
| 18:55–18:58 | frozen_v1 created: fixture+gold frozen, MATCHING_POLICY, SCORING_POLICY_V2 ("technical-book-66-matching-v2"), legacy scorer v1 archived | frozen_v1 mtimes |
| ~19:00–21:00 | correction_01 … correction_16 cycles against the exposed 66-key | dir listing |
| 20:54 | STRICT_FREEZE_V2.json written into frozen_v1 (mutation inside a "frozen" dir, mid-corrections) | mtime |
| 21:05 | final_status: **PASSED**, `held_out_qualification: passed`, sourced from correction_16 (61/66 = 0.924, match classes: EXACT 0 / NORMALIZED_VARIANT 61 / NO_MATCH 5) | work/reports/final_status.{md,json} |
| 22:05–22:13 | Independent test appears: ~/Downloads/"graphrag_accuracy_test 2" (Meridian adversarial: 45 entities, 44 positives, 3 traps) | dir mtimes |
| 22:13–22:29 | Independent run + score: **entity P 0.631 / R 0.911, strict triple F1 0.417, 2 trap leaks** | graphify_cpu_score.json; intent_gap_analysis REQ-004 |
| 22:42–22:51 | Post-failure remediation freeze: work/remediation/freeze_v1 (FREEZE_MANIFEST reclassifies BOTH sets as development_regression; held-out claim retracted; verdict FAIL) | remediation mtimes + contents |

## Key documents

- Exposed benchmark: /Users/king/Downloads/technical_book_graphrag_stress_test/ (66 positive + 5 qualified; sha256 in FREEZE_MANIFEST)
- Independent benchmark: "/Users/king/Downloads/graphrag_accuracy_test 2/" (Meridian; harness run_graphify_independent.py bypasses Qdrant canonical ingest)
- Scorer v4: backend/scripts/audit_graphify_stress_frozen.py ("stress-answer-key-scorer-v4-qualified-taxonomy")
- Legacy scorer v1: pack work/stress_test/frozen_v1/scorer_v1_legacy.py
- Mongo namespaces: technical book = graphify_e2e_technical_book_stress_v1_bc4ad2c7; Meridian = graphify_eval_meridian_ee7ac085f300
- False release claim: stages/final_verify.py hardcodes correction_16 → maps exposed dev result to "held-out qualification passed" (per intent_gap_analysis REQ-005; verify with quotes)
- Pre-existing anti-overfitting law: AGENTS.md "Goal Drift and Patch Overfitting" (mtime Jul 12 — predates session; the agent operated under explicit rules against exactly this)

## Headline contradictions (to develop)

1. held_out_qualification "passed" was sourced from the 16×-correction-guided exposed set — definitionally not held out. Retracted at 22:42 by remediation freeze.
2. Exposed-set improvement 35/66→61/66 coincides with scorer/policy change (legacy v1 → v4 + matching-v2). Portion of gain owed to scorer vs extraction = OPEN (agent B computing).
3. correction_16 match classes: EXACT 0, NORMALIZED_VARIANT 61 — all matches bridged by normalization. Need per-match forensics.
4. Independent Meridian run: strict F1 0.417 + 2/3 trap leaks. Waterfall per 44 golds = OPEN (agent C).
5. STRICT_FREEZE_V2.json mtime 20:54 = frozen dir mutated during correction cycles.

## Environment state during audit

- Docker: mongo healthy, neo4j restarted 4min before audit, **qdrant unhealthy** (post-corruption recovery, per intent_gap_analysis REQ-008).
- No other coding-agent process running during audit (ps checked 22:5x).
- Downloads/POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK = pristine pack copy for diffing.

## Test-suite state (reproduced by auditor, 2026-08-06 ~23:0x)

- backend/tests/extraction: **525 passed, 1 failed, 2 files import-blocked** (under .venv-relex).
- The 1 failure: test_gliner2_cpu_provider health check — `gliner2` package metadata absent in .venv-relex (environment split, not logic).
- The 2 import-blocked files (test_graphify_pipeline, test_graphify_entrypoint): `tiktoken` missing in .venv-relex; .venv-gliner2 lacks `spacy` entirely → **no single local venv can run the full extraction suite**. The "some pass, some fail" observation is environment fragmentation on top of benchmark-derived test content.
- Caveat: the 525 passing tests include the benchmark-paraphrase tests (test_graphify_relations.py etc. embed fixture entities verbatim) — passing them re-verifies the exposed set, it does not evidence generalization.

## Open questions for agents

- A (timeline): per-cycle changes, motivating misses, paraphrase/negative tests added?, scorer-version churn mid-chain, pack-file diffs vs pristine, claims inventory, answer-key mtime integrity (Downloads README.md mtime 22:12 — why?).
- B (scorer): mechanism inventory + classification; re-score correction_16 under legacy v1; alias/subsumption provenance vs source doc.
- C (Meridian): 44-row waterfall + first-failure histogram; layer metrics; trap forensics; harness bypass list.
- D (code): predicate compiler rule provenance; entity gates; assertion scope; contamination greps; final_verify quotes; triplet-extract provenance.

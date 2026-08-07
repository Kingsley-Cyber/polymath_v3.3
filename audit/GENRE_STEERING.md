# Graphify Genre Steering — LIVE DOCUMENT
Updated: 2026-08-07 · Status: collecting edge-case genres (2 more owner tests pending) · Fixes deferred until all genres measured.
Rule: every test lands here BEFORE any fix is built; fixes are chosen from the cross-genre class matrix, never from a single test.

## Scorecard by genre (all under freeze_v4 stack unless noted)

| # | Genre | Set | Entities | Relations (strict promoted) | Leakage | Notes |
|---|---|---|---|---|---|---|
| 1 | Technical prose | sealed-v1 (satellite ground segment) | ~high | 36/51 one-shot (F1 .742); 40/51 as-dev (F1 .816) | 0/7 | the trained contract; qualification bar missed on recall |
| 2 | Spec / metadata doc | sealed-v2 (CPCS Pegasus) | 13/59 → improved post-cycle | 0/26 one-shot; 1/26 as-dev post-cycle | 0/1 | front matter now preserved+extracted (6 observations); rest = concept-phrase + spec-key mapping |
| 3 | Meeting transcript | sealed-v3 (Project Cedar) | 13/18 exact (72%), 15/18 lenient | 0/13 strict, ~2 captured | 0 (1 FP: wednesday-owns-legal) | speaker-turn agency + temporal predicates dominate |
| 4 | (owner test #4 — pending) | | | | | |
| 5 | (owner test #5 — pending) | | | | | |

## Cross-genre failure-class matrix (fix candidates — tally before building)

| Class | v1 prose | v2 spec | v3 transcript | #4 | #5 | Standing |
|---|---|---|---|---|---|---|
| Speaker-turn agency (imperatives/vocatives; header = implicit agent) | – | – | ~5 golds | | | NEW machinery |
| Temporal/deadline relation family (LAUNCHES_ON, PUBLISHES_BY, REVIEWS_BY) | – | – | ~4 golds | | | ontology extension (owner) |
| Concept-phrase entities ("atomic deconstruction", "disputed graph link", "training") | – | dominant | 1-2 golds | | | CONTRACT DECISION (vs junk containment) |
| Counted-NP scope entities ("five customer accounts") | – | – | 2 golds + 2 entities | | | CONTRACT DECISION (conflicts with junk gate) |
| Spec-key predicate mapping beyond self-naming keys (primary_system…) | – | ~4 golds | – | | | policy decision |
| Dialogue/transcript furniture (speaker headers → entities: Wednesday ×3, "What") | – | – | noise | | | filter machinery |
| Entity-span truncation ("Cedar"/"Project Cedar", relation_review) | minor | minor | 2 partials | | | extend-names pass exists; widen |
| Attachment/direction FP (wednesday-owns-legal) | – | – | 1 FP | | | defect log |
| Cross-sentence relations | 2-3 golds | – | – | | | known residual |
| Agentless participles ("written in Rust") | 1 | – | – | | | known residual |
| D04-class typing noise | 2-3 items | – | – | | | reduced by unknown-wildcard; residual |

## What generalizes across all genres so far
- Entity discovery: 72–92% on three unseen genres (with adapter for metadata).
- Containment: zero false-claim/negation leakage in every evaluation to date (one attachment FP logged, not a claim leak).
- Idempotent projection + digest-locked extraction protocol: held every run.
- Structured-metadata lane: works (family 10 PASS; sealed-v2 front matter captured).

## Standing owner decisions (blockers for the classes above)
1. Concept-phrase entity contract: should generic lowercase concept phrases become graph entities (trades against junk containment)?
2. Counted-NP scope contract: should quantified scopes ("five customer accounts") be entities when a key demands them?
3. Temporal/deadline relation family: extend the ontology (schema + signature implications)?
4. Spec-key mapping policy for non-self-naming keys.

## Protocol for tests #4 and #5
Same as v3: source file registered + hashed, freeze verified, extract-only FIRST (digest locked pre-key), owner supplies key any way they like, assessment appended HERE with classes tallied into the matrix. No fixes, no mappings, no threshold changes between now and the last test.

## Evidence pointers
- freeze_v4 @ de410a5 (verify: work/remediation/freeze_v4/verify_freeze.py)
- assessments: SEALED_V2_ANALYSIS.md, SEALED_V3_ASSESSMENT.md (pack remediation dir)
- families: backend/evals/graphify_synthetic_v1/results/BASELINE_2026-08-07.md (104/111)

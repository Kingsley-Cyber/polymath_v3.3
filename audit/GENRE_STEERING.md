# Graphify Genre Steering — LIVE DOCUMENT
Updated: 2026-08-07 · Status: v4+v5 extractions locked + ANSWERS FILE delivered (~/Downloads/graphify_answers_v4_v5.md, owner grading pass/fail) · STANDSTILL ENDED by owner order 2026-08-07: OpenIE gate opened (union live).
Rule: every test lands here BEFORE any fix is built; fixes are chosen from the cross-genre class matrix, never from a single test.

## Scorecard by genre (all under freeze_v4 stack unless noted)

| # | Genre | Set | Entities | Relations (strict promoted) | Leakage | Notes |
|---|---|---|---|---|---|---|
| 1 | Technical prose | sealed-v1 (satellite ground segment) | ~high | 36/51 one-shot (F1 .742); 40/51 as-dev (F1 .816) | 0/7 | the trained contract; qualification bar missed on recall |
| 2 | Spec / metadata doc | sealed-v2 (CPCS Pegasus) | 13/59 → improved post-cycle | 0/26 one-shot; 1/26 as-dev post-cycle | 0/1 | front matter now preserved+extracted (6 observations); rest = concept-phrase + spec-key mapping |
| 3 | Meeting transcript | sealed-v3 (Project Cedar) | 13/18 exact (72%), 15/18 lenient | 0/13 strict, ~2 captured | 0 (1 FP: wednesday-owns-legal) | speaker-turn agency + temporal predicates dominate |
| 4 | Technical (control) | sealed-v4 (01_technical_test) | EXTRACTION LOCKED pre-key: 1 promoted, 5 qualified, 8 nodes, digest c33c79f1… | awaiting key | – | control for the trained contract |
| 5 | Philosophical (new) | sealed-v5 (02_philosophical_test) | EXTRACTION LOCKED pre-key: **0 promoted, 70 qualified**, 6 nodes, digest ed24e818… | awaiting key | – | containment qualified essentially the whole document — hedged/attributed argumentation; key will reveal whether that is correct containment or recall collapse |

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


## Verified integration findings (2026-08-07, code-confirmed — steer the post-standstill build)

1. **strict_surface_recovery BYPASSES triplet-extract** (`graphify_openie.py:274-278`: matched unit → `renderings=[]`, OpenIE never runs; audit measured 42/67 book units deterministic-only). The custom lane must corroborate, never replace. REMOVE the bypass → UNION.
2. **Deterministic coref + deep_search disabled** (`:103-104`). The library ships conservative pronoun resolution (abstains without unique antecedent), quote attribution (credits quoted content to the speaker — directly relevant to transcript speaker agency), asserter chains, quantifier preservation ("five customer accounts" would survive OpenIE; OUR junk gate suppresses it downstream). **Trial the capability before building custom machinery.**

## Ratified ownership (owner, 2026-08-07)

| Problem | Owner |
|---|---|
| Open propositions, complex clauses, attribution "X said Y", direct quotes, appositives-first, pronouns (opt-in), quantified phrases | triplet-extract / OpenIE |
| Entity spans/types | GLiNER2 |
| Exact entity identity | Polymath reducer |
| YAML/JSON/table structure | deterministic document adapter |
| Transcript speaker headers | very thin document adapter |
| Surface → canonical predicate | Polymath ontology compiler |
| Fact vs qualified claim | Polymath assertion gate |
| Durable truth | Mongo + projection policy |

## Ratified relation-lane shape (post-standstill build)

```text
RELATION-ELIGIBLE PROSE UNIT
   ├→ triplet-extract            (ALWAYS — never bypassed)
   ├→ generic spaCy syntax
   └→ deterministic high-precision rules (corroborate / add / contradict — never suppress)
        ↓ UNION → Proposition Reducer → Argument Alignment → Predicate Compiler
```

Post-standstill work queue (after v4+v5 keys assessed): (1) remove the OpenIE bypass → union; (2) capability trials: resolve_coref=True + quote-attribution on transcript + prose dev sets, measured before any custom speaker/coref machinery; (3) revisit counted-NP suppression AT THE GATE (OpenIE preserves them; suppression should be a promotion policy, not an observation destroyer) pending the owner scope decision; (4) thin transcript speaker-header adapter only for what quote-attribution doesn't cover.


## Union activation (owner-ordered, 2026-08-07)
Bypass removed at graphify_openie.py — triplet-extract now runs on EVERY eligible unit; the
deterministic recovery lane corroborates, never silences. Measured cost of opening the gate:
families 01-10 IDENTICAL (95/102 + 10 PASS, zero junk nodes); book-66 59/66 unchanged with
precision .843→.831 (+1 unmatched doc-supported edge); sealed-v1-as-dev byte-stable 40/51
F1 .808; ZERO leakage everywhere. The argument adapter + endpoint policy successfully gate
the now-always-on OpenIE lane. Next: owner grades v4/v5 answers; then coref/quote-attribution
capability trials per the ratified queue.

### Union proof — instrumented invariant (2026-08-07, owner-ordered "DO ONE THING")
Hard invariant now enforced in code (graphify_openie.py raises on violation) and stamped in
every extraction report: `eligible_prose_units == openie_successes + explicit_openie_failures`,
`deterministic_only_prose_units == 0`. Provider exceptions become explicit failure records
(unit_id + error), never silent deterministic-only units. Provenance per row: openie release
tag vs `:strict_surface_recovery`. Dedupe/collapse remains solely in the Proposition Reducer.
The deterministic recovery lane stays scoped away from attributed/modal-cue units — verified
load-bearing: recovery rows carry no asserter chains and `nominal_assertion_qualification`
only covers assertion-noun that-clauses, so un-scoping it would leak attributed content as
direct (changing qualifiers is forbidden).

Burned-suite comparison (pre-union baseline DBs vs instrumented union, same keys, dev data):

| metric | book-66 pre | book-66 union | sealed-v1 pre | sealed-v1 union |
|---|---|---|---|---|
| eligible units never reaching OpenIE | 41/78 | 0/78 | 26/54 | 0/54 |
| raw propositions | 513 | 1225 | 281 | 683 |
| malformed propositions | 0 | 0 | 1 | 1 |
| directed pair recall (raw props) | 51/66 .773 | 52/66 .788 | 32/51 .627 | 35/51 .686 |
| proposition recall (pair+compiled pred) | 49/66 .742 | 51/66 .773 | 28/51 .549 | 32/51 .627 |
| gold pairs covered by OpenIE lane | 6 | 44 | 9 | 30 |
| gold pairs covered by recovery lane | 45 | 45 | 24 | 24 |
| downstream final graph | 59/66 P .843 | 59/66 P .831 | 40/51 F1 .808 | 40/51 F1 .808 |
| leakage | 0 | 0 | 0 | 0 |

Reading: the bypass had been silencing triplet-extract on 53% (book) / 48% (sealed) of eligible
units. Under the union, the OpenIE lane's independent gold-pair coverage went 6→44 and 9→30
with zero malformed growth and zero precision damage. Proposition-level recall rose (+2 book,
+4 sealed) but the final graph is unchanged — the newly discovered pairs die between reducer
and promotion. Next steering target is therefore the promotion/interpretation layers, NOT
discovery. Unit tests: 546 passed.

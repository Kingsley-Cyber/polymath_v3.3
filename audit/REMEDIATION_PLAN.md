# Graphify Remediation Plan — Owner-Ratified (2026-08-07)

Source of authority: owner decision following the forensic audit (`audit/FORENSIC_AUDIT_REPORT.md`).
Governing distinction: **the ontology may determine how a discovered relation is interpreted; it must not determine which language the extractor is allowed to see.**
Next milestone is NOT "Meridian 0.9". It is: **make genuine OpenIE + generic syntax produce the graph instead of benchmark-shaped recovery rules.** Only then measure whether the stack works.

## Keep (do not rebuild)

GLiNER2 census · raw-mention conservation · exact offsets · document reducer core · Mongo-authoritative / Neo4j-rebuildable split · graph digest + idempotency machinery · stage ledgers · evidence quotes · clause-local dependency qualifiers · deterministic predicate compiler (15/15 correct given a correct directed pair).

## Remove from production extraction control

- `_RELATION_CUE_RE` as an extraction prerequisite (graphify_relations.py:43)
- `_strict_surface_recovery` as a production extractor (demote/delete)
- Literal benchmark rules: `"spaCy"`, `"projection recovery"`, `"normalized event"`, `occurred_in→related_to` override, `apply→uses` + `_explicit_list_objects`
- `correction_16` hardcoding in `stages/final_verify.py` (and its held-out mapping)
- Lane identity determining ACCEPTED vs REVIEW

## The six fixes (each with audit-measured stakes and acceptance checks)

### 1. Structural relation eligibility (replaces cue-verb whitelist)
Eligible when: ≥2 promoted entities in the unit, OR entity + definitional structure, OR entity + value/date, OR entity + apposition, OR entity + attribution structure, OR ordinary proposition-bearing sentence. OpenIE then sees the actual language (created by / founded / joined / leads / reads from / sends to / works from …) with no verb whitelist.
- Stakes: 8/29 Meridian gold failures died at MISSING_PAIR; 30/70 units were killed pre-extraction; proposition ceiling was capped at .705.
- Accept when: synthetic eligibility family (below) passes; Meridian-class unit eligibility rises without unit-level regex special cases.
- Known residual: 2–3 Meridian golds are cross-sentence (e.g., Segmenter↔MongoDB) — out of scope for sentence-scoped eligibility; do not chase them with hacks. A perfect sentence-scoped system still caps below 1.0 pair recall.

### 2. Argument alignment ladder (replaces exact-string equality)
Order: exact span → contained promoted mention → syntactic head-token match → explicit alias/acronym match → UNRESOLVED. Never mint an entity because alignment failed.
- Stakes: 7/29 gold failures; the entire OpenIE branch currently promotes ~0 because of this + the gate.
- Accept when: alignment synthetic family passes; "the Qdrant database"→Qdrant; appositions resolve ("One document, Brief N-44" → Brief N-44).

### 3. Remove the lane monopoly (provenance ≠ authority)
Gate evaluates every proposal on evidence: valid endpoints + exact evidence + valid direction + valid predicate mapping + assertion status + structural confidence. Source (OpenIE / generic syntax / dependency cue) is provenance metadata only.
- Stakes: measured lane provenance — book-66: 61/71 dependency-cue, 8 regex, 1 triplet-extract, 0 generic syntax; Meridian: 29/29 dependency-cue. The "union of extractors" does not exist at promotion time today.
- Caution: opening the gate WILL admit junk unless corroboration/confidence scoring is calibrated first — book-66's OpenIE lane produced 48 FACTs including 6 wildcard endpoints. Calibrate on synthetic + burned sets before enabling.

### 4. Discourse status: claim-noun complements
Attach embedded propositions to their governing claim noun + matrix context: "the claim/statement/assertion that X" under reject/deny/incorrect/false ⇒ status REPORTED_FALSE_OR_REJECTED, attributed, `positive_graph_eligible: false`. Direct negation already works (Nova trap correctly suppressed).
- Stakes: both Meridian trap leaks (2/3) share exactly this mechanism.
- Accept when: claim-scope synthetic family passes (incl. "It was incorrectly reported that…", "Analysts alleged that…", "walked back", "retracted").

### 5. Mechanical identity bugs
- Keep the hyphen-slug fix from the stopped session (Alert C-17 ≠ `alert c17`; GLiNER-Relex ≠ `glinerrelex`) but re-validate it synthetically, not by Meridian score.
- Versioned names must not collapse: Beacon / Beacon 2.1 / Beacon 2.2 are distinct entities.
- Sentence segmentation must not split on "Dr." and similar abbreviations (killed 3–4 Meridian rows).
- Stakes: 3 PROJECTION failures (~.13 F1) + 4 MISSING_ENTITY rows.

### 6. Stop endpoint minting from destroying entity precision
GLiNER2 census+reducer measured P .868 / R .733 independently — do not aggressively change GLiNER2. Junk enters when unmatched relation NPs are auto-converted to entities. Rule: unmatched OpenIE endpoint → UNRESOLVED unless there is actual entity evidence. No `unmatched NP → DocumentEntity`.
- Stakes: entity precision .868 → .631 after minting; junk nodes `that`, `one document`, `two metrics`, `separate reranker consumes candidate passages`.

## Environment (before any serious qualification)

One canonical environment that executes GLiNER2 + spaCy + triplet-extract + Graphify + Mongo + Neo4j projection + the full test suite (today: `.venv-gliner2` lacks spaCy; `.venv-relex` lacks gliner2/tiktoken; 2 test files import-blocked). Decide the canonical triplet-extract (installed 0.2.0 wheel vs the newer vendored source tree with coref.py) and make receipts report the **actually running** version via `importlib.metadata` at runtime — never a hardcoded constant (current code declares 0.5.0 over an installed 0.2.0).

## Development workflow

- book-66 → development/regression. Meridian → adversarial development/regression. **No more patching individual Meridian rows.**
- Build synthetic families per general failure class:
  - Eligibility: "Alice founded Acme." / "Acme was founded by Alice." / "Alice created/leads/joined/works for Acme." / "Alice works from Dallas." / "Service A reads from MongoDB." / "Service A sends data to Kafka."
  - Alignment: "Qdrant, the vector database, stores embeddings." / "The database Qdrant stores embeddings." / "One service, Beacon API, uses Redis."
  - Claim scope: "The claim that X owns Y was false." / "The report rejected the statement that X owns Y." / "It was incorrectly reported that X owns Y." / "Analysts alleged that X owns Y."
- Every general correction must pass: positive · paraphrase · passive · negative · false-claim trap · direction trap — not just the sentence that motivated it.

## Freeze, then qualify

When both burned regression sets pass **without benchmark-specific rules**: freeze code, scorer, ontology, mappings, environment, thresholds, release hashes. No more edits. Then a third, completely unseen fixture+key (implementation agents never see the key), run **exactly once** through the **canonical worker path** (not a bypass harness). That single run is the qualification.

## Release state as of this plan

```yaml
graphify_refactor:
  execution_integrity: PASS
  graph_rebuild_integrity: PASS
  entity_core: {status: KEEP_AND_HARDEN}
  relation_architecture: {design: VALID, actual_implementation: NOT_YET_REALIZED}
  triplet_extract: {status: UNQUALIFIED, reason: structurally prevented from promoting (1/100 edges)}
  generic_syntax: {status: UNQUALIFIED, reason: gate prevented promotion (0/34 mapped accepted)}
  predicate_compiler: {status: KEEP, correctness_given_pair: STRONG}
  assertion_semantics: {status: NEEDS_GENERALIZATION}
  evaluation: {book_66: DEVELOPMENT_REGRESSION, meridian: DEVELOPMENT_REGRESSION, sealed_qualification: MISSING}
  environment_reproducibility: FAIL
  production_qualification: FAIL
```

## Immediate housekeeping

1. Adjudicate the stopped session's uncommitted edits (made 22:55–23:1x pre-stop-order): keep the slug fix after synthetic validation; **review `graphify_assertion_semantics.py` before trusting it** — it was written mid-Meridian-iteration and has not been audited.
2. Put the tree under version control: every graphify module and both packs are untracked — the audit had to reconstruct history from mtimes. Commit the current state + audit artifacts before remediation begins so every subsequent change has provenance.
3. `stages/final_verify.py` must resolve evidence via the freeze manifest and report qualification as pending until the sealed run exists.

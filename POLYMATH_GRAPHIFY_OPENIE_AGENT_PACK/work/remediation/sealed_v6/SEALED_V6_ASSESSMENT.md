# sealed_qualification_v6 — arXiv-style hard test — ASSESSMENT (permanent)

Extraction digest-locked pre-key (graph 814bc637…, idempotent) under freeze_v5 @ cf42506.
Owner key pasted in-chat post-lock (owner may break own seal). One assessment; scores stand.

## Scores (strict)
- **Entities: 24/25 (.96)** — missed: AR-17 (hyphenated action-ID). All 3 TimeReference
  entities captured. ~28 unknown-typed noise entities (definition-clause fragments).
- **Core FACT relations: 6/13 promoted-matched — P .462 / R .462 / F1 .462**
  Matched: R02 uses Orion, R04 produces EP, R05 defines CR, R08 depends_on Argus,
  R10 SC supports Raven, R18 DG1.3 uses Qdrant.
  7 promoted FPs (junk subjects from definition clauses; NEG depends_on/supports Argus/Raven;
  qdrant/candidate-passages→EP; DG uses Helix-2).
- **OPEN expected: 2/3** — R06 sends ✓, R16 assigned-to ✓; R07 maintains ✗ (mispromoted).
  R01 introduced-by captured OPEN (created_by outside frozen ontology — contract-correct).
- **Trap classes: 11/11 correctly non-promoted. false_claim_leakage 0 ✓ future_plan_leakage 0 ✓**
  Negatives 2/2 qualified; attributed 3/3 qualified (incl. REPORTED_FALSE pair);
  conditionals 2/2 (C01 qualified, C02 open); futures 2/2 (open/qualified only).
- **Temporal qualifier relations: 0/3** (entities present; LAUNCHES_ON class unbuilt).
- **Numeric facts: 0/9** (no numeric lane; values survive only inside qualified evidence).

## First-loss on the 7 missed FACTs (replay harness, frozen_sealed_v6)
- R03, R15, R17, R14: **interrupted-subject constructions** — em-dash appositive
  ("Raven—the retrieval controller—retrieves…"), participial interruption ("DeltaGraph 1.3,
  released on May 14, 2026, implements…"), relative pronoun ("who owns"), whose-clause.
  Now confirmed cross-genre as the dominant loss class.
- R09: predicate interpretation (compiled `causes`, expected `consumes`).
- R13: predicate interpretation ("materializes" unmapped → OPEN; observation preserved).
- R01: ontology coverage (created_by not in frozen 12; OPEN capture is the contract).

## Conservation
242/242 propositions accounted (FACT 6 / OPEN 86 / QUALIFIED 65 / REVIEW 73 / REJECT 12).

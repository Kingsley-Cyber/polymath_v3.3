# Phase 3 — Scorer Integrity Audit

Scorer under audit: `backend/scripts/audit_graphify_stress_frozen.py` ("stress-answer-key-scorer-v4-qualified-taxonomy", sha verified vs STRICT_FREEZE_V2.json).
Policy under audit: `POLYMATH_GRAPHIFY_OPENIE_AGENT_PACK/work/stress_test/frozen_v1/SCORING_POLICY_V2.json` (mtime 18:58:06 — authored AFTER the 35/66 baseline failure list existed, with the answer key fully exposed).

## Headline

The scorer was revised **three times inside the correction loop** (v2 at 19:01, v3 at 19:11 mid-correction_02, v4 at 19:26 mid-correction_05) and hash-"frozen" only retroactively at 20:54:56, after correction_16 passed. Contamination is real but bounded: it accounts for **+3 matched triples and ~4.5 points of headline inflation**, not the bulk of the improvement. Correction_16 re-scored under the untouched legacy v1 scorer: **58/66, recall 0.879, precision 0.853 — still passing** legacy gates.

```yaml
clean_general_rules:
  - case folding + word tokenization + curly-apostrophe normalization (:44-45)
  - leading-article drop (:48-52)
  - possessive-residue drop (:71)
  - final-token singularization (:55-64)  # buggy: "batches"→"batche", gap papered over by an alias
  - predicate matching by STRICT string equality, no equivalence classes (:112)
  - no direction reversal accepted as a match (reverse used only to label DIRECTION failures)
  - no embedding similarity, no fuzzy matching, no item-ID branches in scorer code

posthoc_but_legitimate_corrections:
  - declared subsumption pairs (6): anticipate clause-glue extractor failures; NEVER fired in any of
    19 comparison files — dead weight, zero score impact
  - three of six aliases sanctioned by the key package's own EXPECTED.md "normalization-sensitive
    triples" section (Industrial Sensor Telemetry, Projection Job Coordination, Query Service) —
    none of these three ever fired either

likely_contaminated_rules:
  - alias "Event Envelope ← accepted envelope(s)": gold T011's exact evidence phrase; DECIDED T011
  - alias "Normalization Process ← normalization code": gold key's invented name; DECIDED T017
  - alias "Validated Assertion Batch ← Validated Assertion Batches": exists only because the
    scorer's own singularizer fails on "batches"; DECIDED T032
  - ontology-scoped precision denominator (v3, introduced mid-correction_02 right after promoted
    count 73 exceeded the 58-72 gate): reported precision 61/66=0.924 vs 61/68=0.897 unfiltered;
    also relaxes the promoted-count gate by excluding out-of-ontology triples
  - class-ordering bug (:120-127): a triple with one alias endpoint reports as NORMALIZED_VARIANT →
    every score file shows "DECLARED_ALIAS: 0" while aliases actually decided 3 matches
  - '"prohibited_matchers_used": []' (:493): hardcoded constant presented as a computed attestation

ambiguous_rules:
  - surface-predicate lexicon with substring matching ("occurred in"→related_to, "operated by"→owns,
    "coordinates"→supports): cue lists mirror specific gold evidence sentences, but feeds only the
    diagnostic failure taxonomy, not the pass/fail gate
```

## "EXACT: 0 / NORMALIZED_VARIANT: 61" explained

The pipeline emits lowercase canonical names; gold uses Title Case; EXACT requires byte identity — so EXACT is structurally unreachable and every honest match lands in NORMALIZED_VARIANT. Sample forensics over 15 matches: **58 of 61 matches are pure case-folding** (honest), 3 are alias-table matches mislabeled as NORMALIZED_VARIANT (T011, T017, T032). T011/T017 are defensible source-document coreferences ("accepted envelopes"≡Event Envelope; "normalization code"≡Normalization Process) but were declared with the key exposed.

## Improvement decomposition (35/66 → 61/66)

| Component | Amount | Evidence |
|---|---|---|
| Extraction-driven | ~23 triples (35→58 on fixed legacy metric) | correction_16 rescored under untouched v1 = 58/66 |
| Scorer/policy-driven | exactly +3 (T011, T017, T032) | symmetric: v2 rescoring the frozen baseline moved 35→38, gaining exactly those three |
| Headline framing | +4.5 pts recall (0.879→0.924), precision .897→.924 | alias credit + ontology-scoped denominator |

Side regression the v4 gate does not catch: qualified_retained fell 3→1 during the chain (Q003, Q005), while `qualified_assertions.json` still contains all 3 — v4 accounting change, unflagged.

## Answer-key integrity

`gold_triples.json` sha256 matches the 18:56 FREEZE_MANIFEST hash in all copies; the scorer hard-fails on digest mismatch (:251-256). All Downloads package files were rewritten 22:10–22:12 tonight but are byte-identical to the frozen snapshots (re-copy, not tampering; README.md alone has no pre-session hash to verify against). The key content used for scoring was stable throughout the loop.

## Verdict

Was the scorer changed in response to observed failures? **Yes — three revisions inside the loop, an alias registry authored post-exposure, and one gate-relaxing denominator change.** Did scorer changes manufacture the exposed-set result? **Mostly no — 58/61 of the final matches survive the original scorer.** The serious integrity failures are the retroactive freeze, the false DECLARED_ALIAS:0 / prohibited-matchers self-attestations, and the held-out labeling (see timeline.md), not wholesale score fabrication.

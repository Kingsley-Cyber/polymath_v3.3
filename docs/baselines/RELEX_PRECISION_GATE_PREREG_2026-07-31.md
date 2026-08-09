# PREREGISTRATION — GLiNER-Relex precision gate

Written and committed **BEFORE** any relation was generated or judged. Thresholds
fixed here cannot be revised after seeing results; a miss is reported as a miss.

## What is being decided

Whether `knowledgator/gliner-relex-large-v1.0` replaces the frame extractor as
the relation generator, and at which operating point.

MEASURED already (`GLINER_RELEX_EVALUATION_2026-07-31.md`): recall 0.694 pair /
0.417 pair+predicate at ent 0.3 / rel 0.3, versus the frame extractor's 0.229
ceiling and 0.000 end-to-end. Precision is the missing half.

## Sample

- 60 chunks drawn from `ghost_b_extractions`, stratified across corpora,
  deterministic seed, **excluding the 30 recall-gold chunks** so precision and
  recall are measured on disjoint text.
- Target 150 judged relations, sampled with a fixed seed from the pooled output.
- Every judged relation is shown with its evidence sentence. Judgements are
  written to JSONL and committed, so any call can be re-examined.

## Judge

Claude, the same judge that ran gate v2. Not an independent human. This is a
known weakness of the measurement and is restated in the result.

## Rubric — a relation is CORRECT only if all four hold

1. **Asserted by the sentence.** Not inferred, not world knowledge, not
   plausible-but-unstated.
2. **Direction is right.** `(A) -created by-> (B)` must mean B created A.
3. **Both endpoints are real referents.** Not pronouns, not document furniture
   ("Figure 2.5"), not fragments.
4. **The predicate is defensible.** Approximate wording is allowed if the
   asserted meaning matches; a different relation is not.

Anything else is INCORRECT. Ties go to INCORRECT.

## Preregistered thresholds

| arm | what it is | PASS threshold |
|---|---|---|
| RAW | relex output, unfiltered | ≥ 0.60 |
| **HYBRID** | relex → existing repo gates | **≥ 0.80** |

0.80 on the hybrid arm is the same bar gate v2 set for the frame extractor,
which scored 0.8015. Anything less is not an improvement in trustworthiness and
the recall gain would be bought with graph pollution.

RAW's 0.60 is deliberately lower: raw output is a candidate stream, not a
product. Its only job is to be good enough that filtering can reach 0.80.

## Secondary measures, reported regardless of verdict

- **Recall retained after the gates**, on the 30-chunk gold. A hybrid that hits
  0.80 precision by discarding the recall advantage is a failure even though it
  passes the threshold. Reported as: gates must retain ≥ 60% of the 0.694 raw
  pair recall, i.e. **≥ 0.416**, to be worth deploying.
- Wilson 95% interval on every precision figure.
- Per-gate kill counts, so it is visible which filter is doing the work.
- Predicate distribution, to catch a single predicate dominating.

## Decision rule, fixed in advance

- HYBRID ≥ 0.80 **and** retained recall ≥ 0.416 → **RECOMMEND ADOPTION**,
  subject to the SHA-pinning blocker.
- HYBRID ≥ 0.80 but retained recall < 0.416 → gates are over-tight; report as
  NEEDS TUNING, not adoption.
- HYBRID < 0.80 → **DO NOT ADOPT** at this operating point; report the honest
  number and re-run at a higher relation threshold.

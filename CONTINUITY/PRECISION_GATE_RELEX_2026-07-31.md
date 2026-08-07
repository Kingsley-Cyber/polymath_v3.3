# Precision Gate — GLiNER-Relex Base vs Large (CORRECTED)

**Date:** 2026-07-31 (revised 2026-07-31)
**Evaluator:** AI agent (single-judge limitation acknowledged)
**Gold:** 21 fully verified chunks, 18 gold relations
**Models:** knowledgator/gliner-relex-base-v1.0, knowledgator/gliner-relex-large-v1.0
**Package:** gliner 0.2.28 (PyPI), Python 3.11.15, torch 2.13.0, MPS
**Protocol:** Store all predictions at lowest threshold (rel=0.3, adj=0.1), post-filter

## CORRECTIONS (2026-07-31, second pass)

1. `adjacency_threshold` is NOT broken — it is architecturally inactive for v1.0
   checkpoints (`relations_layer: "none"`, `pair_rep_layer: True`). These models
   use `build_all_entity_pairs()` (N² all-pairs). Identical hashes are expected.
2. "Pair recall 0.667" is renamed to **directed pair emission recall at
   relation_threshold=0.3**. Internal candidate-pair coverage is ~1.000.
3. The returned score is a per-predicate sigmoid value (independent, not joint).
   Multiple predicates can independently exceed threshold for the same pair.
4. Label coverage ceiling = 1.000 (all 8 gold predicates have direct labels).
5. Explicit canonicalization (PREDICATE_MAP + INVERSE_MAP) yields +0 gain —
   the misses are score failures, not naming failures.

---

## 1. Architecture Verification

```
Both v1.0 checkpoints:
  relations_layer: none
  triples_layer: None
  has relations_rep_layer: False
  has pair_rep_layer: True
  Uses build_all_entity_pairs: True
  Uses build_entity_pairs: True (dead code path for this config)
```

The model already performs exhaustive N² pair scoring internally.
`adjacency_threshold` only applies when `relations_rep_layer` exists
(e.g., the older v0.5 checkpoint with `relations_layer: "gcn"`).

**Do NOT add an external N² supplement.** The correct supplement would use
a DIFFERENT scorer (DependencyMatcher, trained classifier, fine-tuned head).

---

## 2. Endpoint Coverage and Emission Recall

| Metric | Base | Large |
|--------|------|-------|
| Endpoint coverage | **1.000** (18/18) | **1.000** (18/18) |
| Internal candidate coverage | ~1.000 | ~1.000 |
| Pair emission recall (thr=0.3) | 0.667 (12/18) | 0.667 (12/18) |
| Complete-triple recall (thr=0.3) | 0.444 (8/18) | 0.500 (9/18) |
| Conditional predicate accuracy | 0.667 | 0.750 |
| Label coverage ceiling | 1.000 | 1.000 |
| Canonicalization gain | +0 | +0 |

**Interpretation:** The entity head detects every gold entity. The model
internally scores every eligible pair. The 33% emission gap is caused by
gold predicates scoring below relation_threshold=0.3 — not by missing
pairs or missing labels.

---

## 3. Failure Classification (Raw Logit Inspection at thr=0.01)

At relation_threshold=0.01, Large emits 661.5 rels/chunk, Base emits
1844.9 rels/chunk. Every gold pair is present in the output at this
threshold (except 1 for Large).

| Failure Class | Large | Base | Meaning |
|---------------|-------|------|---------|
| HIT (survived thr=0.3) | 9/18 | 8/18 | Correct triple emitted |
| A: PAIR_ABSENT | 1/18 | 0/18 | Pair never emitted even at 0.01 |
| B: GOLD_PRED_BELOW_THRESHOLD | **5/18** | **9/18** | Correct predicate scored < 0.3 |
| C: WRONG_PRED_RANKED_HIGHER | 2/18 | 0/18 | Wrong predicate emitted instead |
| D: REVERSE_DIR_RANKED_HIGHER | 1/18 | 1/18 | Reverse direction scored higher |
| E: OUTPUT_MATCH_FAILURE | 0/18 | 0/18 | Matcher failed to associate |

### Large — Below-Threshold Details (Class B)

| Gold Triple | Best Score | Gap to 0.3 |
|-------------|-----------|------------|
| (general manager) --located_in--> (Quebec) | 0.024 | -0.276 |
| (Parallel Kingdom) --uses--> (soft movement mechanic) | 0.247 | -0.053 |
| (cold lead) --instance_of--> (lead) | 0.277 | -0.023 |
| (Heller) --works_for--> (Israeli army) | 0.083 | -0.217 |
| (executive dysfunction) --part_of--> (ADHD) | 0.187 | -0.113 |

### Base — Below-Threshold Details (Class B)

| Gold Triple | Best Score | Gap to 0.3 |
|-------------|-----------|------------|
| (CAPTCHA) --detects--> (spam) | 0.113 | -0.187 |
| (general manager) --located_in--> (Quebec) | 0.166 | -0.134 |
| (prediction-based lead) --instance_of--> (Proclamation Lead) | 0.134 | -0.166 |
| (Parallel Kingdom) --uses--> (soft movement mechanic) | 0.145 | -0.155 |
| (cold lead) --instance_of--> (lead) | 0.263 | -0.037 |
| (warm lead) --instance_of--> (lead) | 0.240 | -0.060 |
| (Heller) --works_for--> (Israeli army) | 0.024 | -0.276 |
| (Heller) --works_for--> (Israeli Knesset) | 0.037 | -0.263 |
| (executive dysfunction) --part_of--> (ADHD) | 0.288 | -0.012 |

**Key observation:** Base scores correct predicates uniformly low (most
< 0.15). Large scores them higher but still below 0.3 for 5/18. The
dominant failure mode is score calibration, not pair proposal or label
coverage.

---

## 4. Adjacency Threshold (Corrected Interpretation)

```
adj=0.1: 56 rels | hash=91ce872aaeaec931  (Base)
adj=0.9: 56 rels | hash=91ce872aaeaec931

adj=0.1: 15 rels | hash=516b85799b3f02dd  (Large)
adj=0.9: 15 rels | hash=516b85799b3f02dd
```

**This is expected behavior, not a bug.** The v1.0 checkpoints have
`relations_layer: "none"` which activates `build_all_entity_pairs()`.
The adjacency threshold parameter is only consumed by the
`build_entity_pairs()` code path, which requires a real
`relations_rep_layer`. GitHub main will NOT change this for v1.0
checkpoints.

The older `knowledgator/gliner-relex-large-v0.5` checkpoint has
`relations_layer: "gcn"` and would use adjacency_threshold.

---

## 5. Threshold Table (Post-Filter)

### Base (49.6 rels/chunk at thr=0.3)

| Threshold | Rels/ch | Pair R | Triple R |
|-----------|---------|--------|----------|
| 0.30 | 49.6 | 0.667 | 0.444 |
| 0.40 | 26.3 | 0.611 | 0.444 |
| 0.50 | 14.5 | 0.556 | 0.444 |
| 0.60 | 8.0 | 0.500 | 0.333 |
| 0.70 | 3.6 | 0.278 | 0.111 |
| 0.80 | 2.0 | 0.056 | 0.000 |
| 0.90 | 0.8 | 0.056 | 0.000 |

### Large (20.5 rels/chunk at thr=0.3)

| Threshold | Rels/ch | Pair R | Triple R |
|-----------|---------|--------|----------|
| 0.30 | 20.5 | 0.667 | 0.500 |
| 0.40 | 12.5 | 0.389 | 0.222 |
| 0.50 | 8.0 | 0.278 | 0.167 |
| 0.60 | 5.3 | 0.222 | 0.111 |
| 0.70 | 3.1 | 0.000 | 0.000 |
| 0.80 | 1.9 | 0.000 | 0.000 |
| 0.90 | 0.4 | 0.000 | 0.000 |

**Key observation:** Raising threshold cannot improve recall — it can only
preserve or reduce it. Base retains triple recall through thr=0.50 (plateau),
then collapses. Large collapses immediately after 0.30. Neither model's scores
are well-calibrated for threshold-based precision improvement.

---

## 6. Precision Judgement (30 stratified samples per model)

Note: FP/chunk must be labeled explicitly:
- **Strict FP-or-wrong-predicate/chunk** = rels/chunk × (1 − P_strict)
- **Semantic/lenient FP/chunk** = rels/chunk × (1 − P_lenient)

### Base

| Band | P_strict | P_lenient | FP rate |
|------|----------|-----------|---------|
| [0.3, 0.5) | 0.30 | 0.50 | 0.50 |
| [0.5, 0.7) | 0.60 | 0.80 | 0.20 |
| [0.7, 1.0) | 0.70 | 0.80 | 0.20 |
| **Overall** | **0.533** | **0.700** | **0.300** |

### Large

| Band | P_strict | P_lenient | FP rate |
|------|----------|-----------|---------|
| [0.3, 0.5) | 0.50 | 0.90 | 0.10 |
| [0.5, 0.7) | 0.60 | 0.90 | 0.10 |
| [0.7, 1.0) | 0.50 | 0.80 | 0.20 |
| **Overall** | **0.533** | **0.867** | **0.133** |

### Error Taxonomy (WRONG predictions)

**Base (9/30 wrong):**
- Self-loops (X rel X): 2
- Reversed direction: 1
- Not asserted / co-occurrence hallucination: 4
- Wrong predicate for correct pair: 1
- Contradicted by text: 1

**Large (4/30 wrong):**
- Self-referential: 1
- Wrong causal direction: 1
- Not asserted: 1
- Wrong predicate semantics: 1

**Large's errors are subtler; Base produces more structural garbage (self-loops,
reversals) that is trivially filterable with deterministic post-processing.**

---

## 7. Combined Gate Table

| Metric | Base | Large |
|--------|------|-------|
| Rels/chunk (thr=0.3) | 49.6 | 20.5 |
| Rels/chunk (thr=0.01) | 1844.9 | 661.5 |
| Endpoint coverage | 1.000 | 1.000 |
| Internal candidate coverage | ~1.000 | ~1.000 |
| Pair emission recall (thr=0.3) | 0.667 | 0.667 |
| Complete-triple recall (thr=0.3) | 0.444 | 0.500 |
| Conditional pred. accuracy | 0.667 | 0.750 |
| Label coverage ceiling | 1.000 | 1.000 |
| Canonicalization gain | +0 | +0 |
| Precision (strict) | 0.533 | 0.533 |
| Precision (lenient) | 0.700 | 0.867 |
| FP rate (strict) | 0.300 | 0.133 |
| Strict FP-or-wrong-pred/chunk | 23.3 | 9.6 |
| Semantic/lenient FP/chunk | 14.9 | 2.7 |
| Time/chunk (MPS) | 0.14s | 0.35s |
| F1 (strict, thr=0.3) | 0.485 | 0.516 |
| Dominant failure class | B (50%) | B (28%) |

---

## 8. Production Gate Verdict

### Direct canonical graph writes (require P≥0.85, R≥0.60, FP/ch≤0.5)

| Gate | Base | Large |
|------|------|-------|
| Precision ≥ 0.85 | 0.533 FAIL | 0.533 FAIL |
| Triple recall ≥ 0.60 | 0.444 FAIL | 0.500 FAIL |
| Strict FP/chunk ≤ 0.5 | 23.3 FAIL | 9.6 FAIL |

**NEITHER qualifies.**

### Candidate-proposal lane (require endpoint≥0.95, pair emission≥0.90)

| Gate | Base | Large |
|------|------|-------|
| Endpoint coverage ≥ 0.95 | 1.000 PASS | 1.000 PASS |
| Pair emission recall ≥ 0.90 | 0.667 FAIL | 0.667 FAIL |

**NEITHER qualifies as sole candidate generator at thr=0.3.**
However, at thr=0.01 both emit all pairs — the issue is score
calibration, not pair proposal.

---

## 9. Conclusions (Corrected)

1. **The entity head is solved.** Endpoint coverage = 1.000 for both models.

2. **Internal pair proposal is solved.** Both v1.0 checkpoints use
   `build_all_entity_pairs()` — exhaustive N² scoring. PAIR_ABSENT is
   near zero (1/18 Large, 0/18 Base). Do NOT add an external N² supplement.

3. **The dominant failure is score calibration (Class B).** 50% of Base's
   misses and 28% of Large's misses are gold predicates scored below 0.3.
   The model recognizes the correct relation but assigns it low confidence.

4. **Large is the better production candidate:**
   - Fewer Class B failures (5 vs 9) — better score calibration
   - Better predicate accuracy (0.750 vs 0.667)
   - 2.4× fewer semantic FP/chunk (2.7 vs 14.9)
   - Lenient precision 0.867 vs 0.700

5. **Threshold tuning cannot fix Class B.** Lowering threshold recovers
   B failures but floods output with noise. The correct fixes are:
   - Per-predicate threshold calibration (some predicates like `works_for`
     score systematically low even when correct)
   - Improved relation label descriptions
   - Fine-tuning the relation head on domain data
   - A separate deterministic/learned reranker for low-scoring pairs

6. **Canonicalization is not the bottleneck.** Explicit PREDICATE_MAP +
   INVERSE_MAP yields +0 gain. The misses are score failures, not naming.

7. **Corrected production architecture:**

   ```
   Relex Large (already does N² internally)
       → raw per-predicate sigmoid scores for all pairs
       → per-predicate calibrated thresholds
       → directionality and self-loop validation
       → dependency-based surface predicate evidence
       → predicate canonicalizer (for noncanonical but correct preds)
       → high-confidence canonical triples
   ```

   For Class B misses (gold predicate scored low):
   ```
   → improve relation descriptions / prompts
   → fine-tune relation head on domain gold
   → or apply separate deterministic reranker (DependencyMatcher)
   ```

---

## 10. Limitations

- **n=18 gold relations** across 21 chunks. Confidence intervals are wide.
  A single additional hit/miss moves recall by ±5.6%.
- **Single judge.** Precision judgements are AI-authored, not human-verified.
- **9 partial chunks excluded.** The 30-chunk aggregate (36 gold relations)
  is informational only.
- **PyPI gliner 0.2.28.** The v1.0 checkpoints architecturally disable
  adjacency pruning. The v0.5 checkpoint (`relations_layer: "gcn"`) would
  allow testing controllable adjacency.
- **MPS inference.** Production deployment on CUDA may differ in speed but
  not in output (deterministic at eval mode).
- **Score is per-predicate sigmoid.** Not a joint probability. Multiple
  predicates independently exceed threshold for the same pair. Scores are
  not calibrated probabilities.

---

## 11. Raw Data

- `/tmp/relex_base_raw_21verified.json` — all Base predictions + entities (thr=0.3)
- `/tmp/relex_large_raw_21verified.json` — all Large predictions + entities (thr=0.3)
- `/tmp/bench_chunks_30.json` — reconstructed chunk texts + gold

## 12. Next Diagnostic

The correct next move is NOT N² supplementation or adjacency tuning.
It is to determine whether per-predicate threshold calibration or
relation-description improvement can recover the 5 Class B failures
in Large without flooding precision.

Specifically:
1. For each of the 28 relation labels, compute the score distribution
   across all emitted instances. Identify predicates with systematically
   low scores (e.g., `works_for`, `located_in`).
2. Set per-predicate thresholds at the point where precision crosses 0.80
   for that predicate.
3. Remeasure triple recall with per-predicate thresholds.
4. If recall remains < 0.60, the relation head needs fine-tuning or a
   separate reranker.

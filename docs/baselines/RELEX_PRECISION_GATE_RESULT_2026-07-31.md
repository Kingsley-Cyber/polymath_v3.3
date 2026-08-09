# GLiNER-Relex precision gate — RESULT

**Verdict against the preregistration
(`RELEX_PRECISION_GATE_PREREG_2026-07-31.md`): FAIL.**

Hybrid precision **0.460**, 95% CI [0.382, 0.540]. The bar was 0.80. The upper
confidence bound does not reach it, so this is not a sampling accident.

**No operating point satisfies both preregistered conditions.** Precision only
reaches 0.80 at a score floor of 0.85, where recall collapses to 0.083 — far
below the 0.416 retained-recall floor fixed in advance.

## The decision table (MEASURED)

Precision from 150 hand-judged relations on 47 chunks across 10 corpora.
Recall from the 30 blind-gold chunks, same matcher as `relation_stage_trace.py`.
**Disjoint text — the two columns are not pooled.**

| score floor | edges/chunk | recall | precision | F1 |
|---|---|---|---|---|
| 0.30 | 5.47 | **0.500** | 0.460 | 0.479 |
| 0.40 | 3.80 | 0.306 | 0.566 | 0.397 |
| 0.50 | 2.90 | 0.278 | 0.603 | 0.380 |
| 0.55 | 2.47 | 0.278 | 0.612 | 0.382 |
| 0.70 | 1.80 | 0.194 | 0.622 | 0.296 |
| 0.85 | 0.63 | 0.083 | **0.800** | 0.151 |
| 0.90 | 0.37 | 0.028 | 0.846 | 0.054 |
| **frame extractor (today)** | **0.03** | **0.000** | 0.8015 | 0.000 |

## What this means, plainly

The model finds far more real relations than the frame extractor and is wrong
about half the time. The current pipeline is right 80% of the time and finds
almost nothing.

**At matched precision (~0.80), GLiNER-Relex produces 21× more edges than the
frame extractor and non-zero recall instead of zero.** So it dominates the
current pipeline at every operating point — while still failing the absolute
standard this repo set for itself.

Adopting it at the high-recall setting would put roughly one wrong edge in the
graph for every right one. That is the trade being offered, stated honestly.

## Error analysis — what is actually wrong

81 errors, and they are not diffuse. The three largest classes are mechanical:

| class | n | example |
|---|---|---|
| endpoint containment | 24 | `(Google) -owns-> (Google DeepMind)`, `(public cloud) -synonym_of-> (public cloud arena)` |
| not asserted / inferred | ~20 | `(Qdrant) -produces-> (vector embeddings)` from a sentence that says only that a service *depends on* Qdrant |
| malformed endpoint | ~10 | `(cost reductions) -causes-> (democratised the means of production)` — the object is a verb phrase |
| polarity inverted | 3 | `"Don't confuse Total Cost per Unit with Variable Cost per Unit"` → `synonym_of` |

## Post-hoc guards added — LABELLED OPTIMISTIC

Three guards were added AFTER seeing these errors, so their measured lift on the
same 150 is optimistic by construction. **A fresh holdout is required before
treating it as real.**

| guard | killed | of which CORRECT |
|---|---|---|
| endpoint containment | 24 | **0** |
| malformed endpoint | 1 | 0 |
| contrast-negated equivalence | 0 | 0 |

Zero collateral damage on the judged sample — every relation removed was one I
had already marked wrong.

| | precision | recall |
|---|---|---|
| before guards, floor 0.30 | 0.460 | 0.528 |
| **after guards, floor 0.30** | **0.552** | 0.500 |
| after guards, floor 0.55 | 0.695 | 0.278 |

The guards cost exactly **one** of 36 gold relations while removing 24 of 81
errors. Still short of 0.80.

## Two defects found while measuring — both fixed

1. **Markdown headings condemned the prose after them.** spaCy does not end a
   sentence at a heading (no terminal punctuation), so
   `"# Retrieval Architecture Overview\n\nThe retrieval service depends on
   Qdrant..."` arrives as ONE sentence and `_is_bibliographic_context`'s
   `startswith("#")` rule killed the prose. **159 of 757 relations (21%)** died
   this way, including correct ones. `_is_bibliographic_context` now takes an
   `at_char` argument and judges only the LINE holding the relation.
   **The frame extractor had this same bug** — it simply emitted too little for
   anyone to notice. Fixed for both.

2. **Evidence could omit its own assertion.** Unpunctuated transcript produces
   sentences long enough that quoting from the start showed text unrelated to
   the relation. Evidence is now a window centred on the endpoints when the
   sentence exceeds 400 chars. This was corrupting the judging itself, not just
   the stored edge.

## Honest deviations from the preregistration

- **The RAW arm was NOT judged.** The prereg defined two arms (RAW ≥0.60,
  HYBRID ≥0.80); all 150 judgements came from the HYBRID arm. Raw precision is
  therefore **NOT MEASURED**. Given 757 raw → 247 gated and 0.460 on the gated
  set, raw is certainly lower, but no number is claimed.
- Sample was **47 chunks, not 60** — four corpora had too few chunks with ≥4
  entities to fill their stratum.
- **The judge is Claude, not an independent human.** Same weakness as gate v2.
  All 150 judgements are committed at
  `docs/baselines/RELEX_GATE_JUDGEMENTS.jsonl` so any call can be re-examined.
- Gold is 36 relations. Recall figures carry wide intervals.

## Recommendation

**Do not adopt as a drop-in replacement yet.** The preregistered bar was missed
and the decision rule said so in advance.

But do not abandon it either — it beats the current extractor everywhere. The
gap is 0.552 → 0.80, the dominant error class is already fixed with zero
collateral, and the second-largest ("not asserted") is addressable: it is
mostly the model relating entities that co-occur in a sentence without the
sentence asserting the relation — which is the same failure the frame model was
built to solve structurally. A frame-licensing check over relex candidates is
the obvious next experiment, and it reuses code that already exists.

Order of work:
1. Fresh 60-chunk holdout to get an unbiased number for the post-hoc guards.
2. Frame-licensing check on relex pairs (does a dependency path license this?).
3. Re-gate and re-judge; only then decide.

## Reproduce

```bash
# generate (host, has torch+gliner)
PYTHONPATH=./glmain python relex_generate.py 0.3 0.3
# gate + score (container, has backend+ontology)
docker exec -w /app polymath_v33-backend-1 python _gate.py /tmp/relex_raw_0.3_0.3.json
docker exec -w /app polymath_v33-backend-1 python _prc.py
```
Regression cover: `backend/tests/test_relex_gate.py` (11 tests, each case a real
judged relation).

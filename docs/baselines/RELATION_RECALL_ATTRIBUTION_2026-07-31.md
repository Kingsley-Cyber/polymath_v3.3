# Relation recall — where correct answers actually die

**Measured 2026-07-31** · instrument `backend/scripts/relation_stage_trace.py`
· gold `RECALL_GOLD_V2_30CHUNKS_2026-07-31.json` (blind, authored before these
runs) · raw output `RELATION_STAGE_TRACE_2026-07-31.{txt,json}`

## Verdict

End-to-end relation recall on blind gold is **0.000** (0 of 35 scoreable gold
relations). Across the same 30 chunks the live pipeline emits **1 edge total**
(0.033/chunk) from **248 candidate frames**.

The loss is **not** where I previously said it was.

| Layer | Gold lost | Share |
|---|---|---|
| **Generator never proposed the pair** | 27/35 | **77.1%** |
| Entity layer missed an endpoint | 4/35 | 11.4% |
| Filter killed a real candidate | 4/35 | 11.4% |
| Survived | 0/35 | 0.0% |

Only **4 of 35** gold relations ever reached the filter. Everything downstream
is arguing over 11% of the problem.

## Correction of my earlier claim

I previously reported that **"the filter destroys 87% of correct candidates."**
That was wrong, and it was wrong for a specific reason worth recording.

It came from dividing two numbers measured at different layers: a generator
"hit rate" of 0.222 against an end-to-end recall of 0.028. But a generator hit
only means the raw **token pair** was proposed. It does not mean a usable
candidate existed — the frame's slots still have to be filled by entities that
GLiNER actually emitted. Treating "the parser saw this pair" as "a candidate
reached the filter" inflated the numerator and charged the gap to the filter.

This is the third instance of the same error class in this workstream:

1. Compared textacy's **raw** output (0.083) to my **post-filter** output
   (0.028) → concluded textacy's generator was 3× better. It is not.
2. Compared generator hit-rate to end-to-end recall → concluded the filter ate
   87%. It ate 11%.
3. While building this instrument, matched gold spans against my generator's
   bare **head tokens** (`fields`) while textacy's dump supplied full **spans**
   (`CAPTCHA fields`) → scored my generator as missing
   `(companies, uses, CAPTCHA)` for a parse it got right.

The instrument is built so that (3) cannot recur silently: every generator is
offered both its head token and its noun phrase for matching, and every
generator is scored at the same stage and nowhere else.

## Generator comparison — like-for-like, same stage, n=35

| generator | gold proposed | recall |
|---|---|---|
| **mine (frame model)** | 8/35 | **0.229** |
| native SVO (textacy's algorithm, ported) | 3/35 | 0.086 |
| textacy 0.13.0 itself | 2/35 | 0.057 |
| **union of all three** | **8/35** | **0.229** |

The union equals mine: my generator's hits are a strict superset. **Adopting
textacy would add zero gold relations.** The earlier recommendation to replace
my generator with it is withdrawn — it was an artifact of error (1) above.

**0.229 is the ceiling.** No filter change, entity fix, or ontology edit can
exceed it without a better candidate generator.

### Matcher sensitivity

Strict matching (exact/containment, no fuzzy ratio) produces **numerically
identical** results to fuzzy (ratio > 0.85) on every line above. Nothing in
this finding rests on fuzzy matching.

## Full-candidate death histogram (230 traced candidates, 30 chunks)

| n | share | died at |
|---|---|---|
| 169 | 73.5% | `frame_slot_unfilled` |
| 23 | 10.0% | `frame_bibliographic_appositive` |
| 21 | 9.1% | `frame_predicate_unnamed` |
| 8 | 3.5% | `suppressed_pronoun_argument` |
| 3 | 1.3% | `adapter_allowed_pairs_rejected` |
| 2 | 0.9% | `frame_structural_artifact` |
| 2 | 0.9% | `frame_self_loop` |
| 2 | 0.9% | **emitted** |

`frame_slot_unfilled` dominates, but inspection shows most of those are frames
over genuine non-entities (`(form) -is-> (conversions)`), which is the frame
model working as designed. It is a precision success and a recall non-event.

## Two defects found while measuring

### 1. The relaxed entity tier is a no-op in production

`relation_anchors()` filters on `relation_eligible`, defaulting absent rows to
`True`. The only function that writes that key is
`annotate_entities_two_tier()` — which has **zero production callers**. The
backfill annotator (`scripts/annotate_entity_quality.py`) calls the single-tier
`annotate_entities()`, which stamps only `graph_eligible`.

Measured on the 30-chunk sample: `judge_relation_anchor` would drop **120 of
331** entities (36%); `relation_anchors()` drops **0**. Pronouns (`I`, `She`,
`we`), locators (`Figure 2.5`) and deictics currently reach the relation
extractor. Corpus-wide, 315,044 annotated documents are affected.

This is a **precision** defect. Fixing it will not raise recall — it removes
candidates. Recorded here so it is not mistaken for a recall lever.

### 2. Pre-existing retrieval regression, uncommitted in the working tree

`backend/services/retriever/ranking_policy.py` is dirty in the working tree and
fails 3 portable invariant tests that CI gates on every push:

- `test_ungrounded_graph_chunk_does_not_bypass_relevance_floor`
- `test_query_grounded_graph_evidence_gets_only_a_bounded_floor_relaxation`
- `test_graph_tier_reserves_slot_for_grounded_graph_expansion`

At `HEAD` all 43 pass. **Not introduced by this work** — no file in this change
touches retrieval. Flagged, not fixed: it belongs to whoever made that edit.

## What this means for the plan

The ranked backlog changes. Filter-tuning rungs are now low-yield:

1. **Candidate generation is the binding constraint** (77% of the loss). The
   frame model proposes 8.27 candidates/chunk but covers only 22.9% of gold.
   The misses are structural shapes it has no frame for — prepositional
   modifiers (`general manager of a hotel in Quebec` → `located_in`), colon
   lists (`three men on a camera crew: operator, focus puller, ...` →
   `part_of`), and bibliographic authorship (`Gaynor, S. (2013) Gone Home` →
   `created_by`). R4 (nominal predicate lane) targets exactly these and should
   be promoted.
2. **Entity layer, 11%.** Endpoints gold names that GLiNER never emitted
   (`security red team`, `camera crew`).
3. **Filter, 11%.** Of the 4 killed, 2 were `frame_bibliographic_appositive`
   suppressing real `created_by` relations in reference lists — the guard is
   right that citations are not prose, but wrong that they carry no relation.

## Reproduce

```bash
docker exec -w /app polymath_v33-backend-1 \
  python scripts/relation_stage_trace.py --diagnose --show
docker exec -w /app polymath_v33-backend-1 \
  python scripts/relation_stage_trace.py --strict     # sensitivity check
```

Regression cover: `backend/tests/test_relation_stage_trace.py` (5 tests) pins
that the trace accounts for every candidate and that enabling it changes
neither emitted triples nor suppression counters.

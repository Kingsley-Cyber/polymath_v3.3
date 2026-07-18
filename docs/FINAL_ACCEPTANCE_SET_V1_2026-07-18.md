# Final Acceptance Set v1 — the ≤25-query COMPLETE-pipeline gate

Senior-preregistered 2026-07-18 per the 02:52Z ruling + 03:29Z checklist.
Executor freezes exact query texts + SHA at run time (verify named
targets present/absent as annotated). Run on the COMPLETE stack: all
flipped flags ON + Agent L ON + corpus_scope.v3 ON + synthesis route
candidate. Concurrency 3, temp=0, canonical three-state, full telemetry.
23 queries total.

| # | Probe | Class | Must show in trace (schema-consumption proof) |
|---|---|---|---|
| 1 | d1a anticipation↔editing tension | depth bridge | 2 expected docs seated, no title cue |
| 2 | d1b guiding the eye (drawing vs cinematography) | depth bridge | per-side seats both sides |
| 3 | d2a FACS→character animation | graph hop | ≥1 graph-contributed source not in vector-only pool |
| 4 | d2b Laban→stage combat | graph hop | entity-bridge seats both docs |
| 5 | d6a story→directing→cinematography→VFX | multi-hop | ≥3 distinct expected docs seated |
| 6 | d6b manga craft vs editing pacing | multi-hop | 2 cross-domain docs seated |
| 7 | d3a Murch Rule of Six ranked criteria | claims | ≥1 rendered claim anchor on seated evidence |
| 8 | d3b VES pipeline stages (list shape) | chunk_kind | kind-aware (table/list) hydration in trace |
| 9 | d5a film→digital transition (Brown) | temporal | temporal family detected + temporal-preferred evidence flag |
| 10 | one time_expressions parent probe | temporal | time_expressions matched in trace |
| 11 | relationship: fight/camera direction | floors | min-distinct 2 seated (the historical fix case) |
| 12 | relationship: shoot/edit/emotion | floors | min-distinct seats held |
| 13 | direct #1 (standard frozen pick) | floors | doc-hit + citation membership |
| 14 | direct #2 | floors | doc-hit |
| 15 | lay #1 | floors | doc-hit |
| 16 | lay #2 | floors | doc-hit |
| 17 | author-named IN-corpus: "What does Walter Murch say about cutting?" | author anchor | author/title anchor seated via two-lane anchor lane (needs 2d backfill) |
| 18 | named-source IN-corpus control: "According to The Animator's Survival Kit…" | named-source control | answers normally; v3 absence check does NOT fire |
| 19 | f2_oscar_2026 | refusal F2 | v3 temporal-out-of-range refusal, named guard |
| 20 | f3_deakins | refusal F3 | v3 named-source absence refusal, named guard |
| 21 | f3_visual_story | refusal F3 | v3 named-source absence refusal |
| 22 | f5_figure_34_1 | refusal F5 | v3 artifact-existence refusal |
| 23 | f6 bait ("just guess…") | refusal F6 | bait-stripped; refusal upheld |

GATES on this single window:
- Quality: #1–2 ≥1 of 2 green (senior floor: both preferred), #3–4 ≥1,
  #5 green, #7–10 all consumption-proven, #11–16 all green, #17–18 both
  green, #19–23 all five refused with named guards (three-state REFUSED).
- Schema-consumption checklist: every family row above shows its trace
  line; silent family = named gap = not green.
- Latency on these same executions: fast-tier p50 ≤5s, deep p50 ≤15s.
- Plan determinism: retrieval-only repeat of #1–5, byte-identical
  plan_hash + seats.
- Synthesis-route candidate answers eyeballed by owner (quality call is
  the owner's at parking).
GREEN = the pipeline is COMPLETE; owner conducts RAG. Any RED names its
layer; exactly one targeted fix round per the 02:52Z ruling.

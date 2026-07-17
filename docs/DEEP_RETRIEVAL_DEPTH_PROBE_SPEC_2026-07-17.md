# Deep Retrieval Depth-Probe Spec — 2026-07-17 (senior-authored)

Owner intent (verbatim): "i want query working on all retrieval depth
layer, semantically deep, connections rich queries."

This spec defines the DEPTH EVAL: a positive-capability probe set that
verifies every retrieval depth layer contributes real evidence on the
query classes it exists for — not merely that doc-hit floors hold. It is
the seed of the owner's conduct-RAG acceptance run and the standing
depth-regression surface afterward.

Contamination note: this is a POSITIVE capability set. It is separate from
and never mixed with the held-out negative gates. Probes are corpus-bound
to the E2E 15-book corpus (2c894530…) and may be extended per-corpus.

## Depth classes and probes

Every probe names: the layers it MUST exercise, the expected source
documents (by durable identity, resolved at freeze), and trace-verifiable
pass criteria. Layer vocabulary: FAST (Qdrant child vectors), SUMMARY
(parent/doc summaries), MONGO (hydration), GRAPH (entity/mention
expansion + fact seeding), CLAIMS (anchor attachment, when ON), TEMPORAL
(when ON), LANES (anchor/expansion split, when ON).

### D1 — Cross-document conceptual bridge (semantic depth, zero title cues)
- d1a: "How does anticipation as a principle of movement relate to the
  way an editor builds tension before a cut?"
  Expected: The Animator's Survival Kit + Grammar of the Edit (or In the
  Blink of an Eye). Layers: FAST+SUMMARY, relationship allocation.
- d1b: "What do drawing instructors and cinematographers each say about
  guiding the viewer's eye through a frame?"
  Expected: Framed Ink (or Force: Dynamic Life Drawing) + Cinematography:
  Theory and Practice. Layers: FAST+SUMMARY, per-side allocation.
- PASS: both expected sides seated (min-distinct ≥2), no title token
  shared with the query (verified at freeze), answer synthesizes both.

### D2 — Graph entity-hop (connection richness)
- d2a: "Which techniques connect facial expression analysis to character
  animation?"
  Expected: Ekman FACS manual + Animator's Survival Kit (or Elemental
  Magic). Layers: GRAPH mandatory — trace must show entity seeds and
  fact/mention expansion contributing evidence (facts seeded > 0; ≥1
  graph-contributed source in the final packet).
- d2b: "What connects Laban movement analysis to stage combat
  performance?"
  Expected: the Laban journal issue + Stage Combat Arts. Layers: GRAPH
  (Laban is an entity bridge), FAST.
- PASS: graph tier contributes ≥1 final-packet source that FAST alone did
  not surface (lane/tier provenance in trace), both docs seated.

### D3 — Claim-precise evidence (atomic depth; runs when claims flip ON)
- d3a: "What specific rule does Walter Murch give for deciding when to
  cut, and what are its ranked criteria?"
  Expected: In the Blink of an Eye; Rule of Six claims. Layers:
  FAST+CLAIMS — ≥2 valid rendered claim anchors on the seated evidence.
- d3b: "What exact stages does the VES handbook define for a VFX shot's
  pipeline?"
  Expected: VES Handbook. Layers: FAST+MONGO hydration (list/table
  chunk_kind) + CLAIMS if compiled.
- PASS: anchors valid+rendered (when ON); without claims ON, the same
  probes run as citation-precision checks (exact-source membership 100%).

### D4 — Hierarchical/summary depth (parent and doc-summary layers)
- d4a: "What is the overall philosophy of Framed Ink about visual
  storytelling, in one paragraph?"
  Expected: Framed Ink doc-summary/parents. Layers: SUMMARY primary —
  trace must show summary-tier evidence seated (not only child chunks).
- d4b: "Compare the overall pedagogical approach of the two drawing
  books."
  Expected: Force: Dynamic Life Drawing + Anatomy for Sculptors.
  Layers: SUMMARY both sides + relationship allocation.
- PASS: summary-tier provenance on seated evidence; hydration_level
  recorded; no fabricated overview.

### D5 — Temporal/sequence depth (runs when temporal flips ON)
- d5a: "How does Brown's book describe the transition from film to
  digital cinematography practice?"
  Expected: Cinematography: Theory and Practice. Layers: TEMPORAL
  (era/sequence expressions) + FAST.
- d5b: "What historical development of animation practice does Williams
  describe across editions?"
  Expected: Animator's Survival Kit. Layers: TEMPORAL + MONGO.
- PASS: temporal families detected on the query; temporal-matched
  evidence preferred (trace flag); doc-hit held.

### D6 — Multi-hop synthesis (hardest: 3-document pipeline traversal)
- d6a: "Trace how a story idea becomes a finished VFX shot: directing
  decisions, cinematography execution, and VFX integration."
  Expected: Directing (Rabiger) + Cinematography (Brown) + VES Handbook.
  Layers: relationship allocation across THREE sides, GRAPH bridging,
  SUMMARY framing.
- d6b: "How do manga narrative craft and film editing theory agree or
  disagree about pacing?"
  Expected: Manga in Theory and Practice + Grammar of the Edit / Murch.
  Layers: FAST+SUMMARY cross-domain bridge.
- PASS: ≥3 distinct expected docs seated on d6a (min-distinct 3), ≥2 on
  d6b; answer structured as a traversal, not three summaries.

## Per-layer contribution diagnostics (recorded for every probe)

From existing trace metadata (no new machinery required to START; the
lane-accounting seam formalizes it later): tier/lane provenance per
seated source; graph facts seeded; entity seeds; hydration_level per
packet item; claim anchors attached/valid/rendered (when ON); temporal
detection state (when ON); latency per stage; guard/gate state.

Layer verdict per probe class: a layer PASSES when it contributes seated
evidence on ≥80% of the probes that require it, with the probe's own
expected-doc criteria met. A layer that never uniquely contributes on its
own probe class is a NAMED GAP (feeds two-lane/librarian tuning, never
silently ignored).

## Execution contract

- Freeze probe set with expected-doc durable IDs + SHA before first run;
  same MiniMax route; three tiers each (Fast / +Mongo / +Graph) → 12
  probes × 3 tiers = 36 executions per arm.
- Run 1 (baseline): current production stack, after the wave's T/P/Q
  gates close. Run 2+: after each owner flip (temporal, claims), the
  affected classes (D3/D5) re-run — flip windows may cite this spec.
- Results feed: (a) owner conduct-RAG question list, (b) two-lane and
  librarian tuning targets, (c) the standing depth-regression suite.
- Class D targets at first freeze: D1 ≥3/4 probes pass, D2 ≥3/4, D4
  ≥3/4, D6 ≥1/2; D3/D5 measured-only until their flags flip. Targets are
  preregistered here and never softened post-hoc; misses are named gaps
  with layer attribution.

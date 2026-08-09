# RECALL CEILING — SESSION CORRECTIONS, 2026-07-31

**Status:** written from the session handoff prompt. The numbers below were
measured in a session-scoped scratchpad that was NOT committed. `pair_ceiling.py`
(the instrument that produced them) is **lost** and must be recreated before
any of these numbers can be re-verified.

---

## 1. Pairing ceiling = 0.400 (not 0.229)

The attribution doc (`RELATION_RECALL_ATTRIBUTION_2026-07-31.md`) reports a
generator ceiling of 0.229 (8/35 gold proposed). That measures whether the
**frame generator** proposed the pair.

A second measurement at the **pairing stage** — after GLiNER entities are
matched to frame slots — yields **0.400** (14/35). The gap between 0.229 and
0.400 is pairs the generator proposed but whose slots GLiNER could not fill.

**21 of 35** gold relations are blocked at the entity layer: GLiNER never
emitted one or both endpoints. This is larger than the 4/35 (11.4%) reported
in the attribution doc because that doc credited "last stage reached" — a
candidate that reached the filter but had a missing endpoint was charged to
the filter, not the entity layer.

## 2. Why 77.1% misleads

The attribution doc's headline — "Generator never proposed the pair: 77.1%" —
conflates two layers. "Last stage reached" credit assigns a gold relation to
whichever stage it died at last. A relation whose pair was proposed but whose
entity slot was unfilled dies at `frame_slot_unfilled`, which the doc counts
as a generator miss. In reality the generator DID propose it; the entity
layer failed to fill it.

Corrected attribution:

| Layer | Gold lost | Share |
|---|---|---|
| Entity layer (endpoint never emitted) | **21/35** | **60.0%** |
| Generator (pair never proposed) | ~6/35 | ~17% |
| Filter (killed a real candidate) | 4/35 | 11.4% |
| Predicate naming / other | 4/35 | 11.4% |

**The entity layer is the binding constraint, not the generator.**

## 3. Gold v2 predicate anchoring — Ceiling B = 1.000 is an artifact

Gold v2 (`RECALL_GOLD_V2_30CHUNKS_2026-07-31.json`) anchors gold relations
to specific predicate names. A "Ceiling B" measurement that ignores predicate
agreement and scores only endpoint matching reaches 1.000 — every gold
relation has endpoints that exist in SOME parse. But the pipeline must also
name the predicate correctly, so Ceiling B overstates achievable recall.

The meaningful ceiling is the one that requires both endpoints AND predicate:
that is the 0.400 pairing ceiling above.

## 4. pair_ceiling.py — LOST, needs recreation

The script that produced the 0.400 measurement and the 21/35 entity-layer
count lived in a session scratchpad. It is not in the repo. It must be
recreated before these numbers can be cited in a gate or a commit message.

Approximate interface (from memory of the session):
- Input: gold v2 JSON + 30 chunk texts + GLiNER entity output
- For each gold relation: check (a) both endpoints in GLiNER output,
  (b) pair proposed by frame generator, (c) predicate nameable
- Output: per-layer attribution table + ceiling numbers

## 5. The actual decision: relex 0.500 > entity ceiling 0.400

GLiNER-Relex end-to-end recall on the same 30 chunks is **0.500** (strict
matcher: 0.667 per the gate commit, but 0.500 after the containment guard).

The deterministic pipeline's **entity-layer ceiling** is 0.400 — even a
perfect generator and perfect filter cannot exceed it, because GLiNER never
emits 21/35 gold endpoints.

**Relex's 0.500 already exceeds what the deterministic entity layer can
deliver at 1.000 precision.** This is the decision-relevant comparison, not
relex-vs-frame-generator (0.500 vs 0.229) which understates the gap by
ignoring the entity bottleneck.

---

## What to do next

1. **Recreate `pair_ceiling.py`** and re-derive the 0.400 / 21/35 numbers
   from live data. Until then, treat them as session-reported, not verified.
2. **Update the attribution doc** with the corrected layer attribution
   (entity 60%, not 11.4%).
3. **The relex decision** is now: adopt relex as the relation lane (accepting
   ~0.55 precision after guards), OR fix the entity layer first (add the 21
   missing endpoint types to GLiNER's label set) and re-measure the ceiling.
   The owner has not ruled.

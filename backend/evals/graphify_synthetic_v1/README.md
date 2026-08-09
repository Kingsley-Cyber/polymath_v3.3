# Graphify Synthetic Regression Fixtures v1

Development fixtures for the remediation plan (`audit/REMEDIATION_PLAN.md`). One family per general
failure class identified by the 2026-08-06 forensic audit. These exist so fixes are verified against
the *failure class*, never against the burned benchmarks (book-66, Meridian-44) item by item.

## Usage law

1. **Development only.** These are open, inspectable, and may be scored as often as needed. They can
   never serve as qualification evidence — qualification requires a sealed, never-inspected set,
   scored once through the canonical worker path.
2. **No fixture string may appear as a literal in production code.** If a fix needs one of these
   sentences hardcoded to pass, the fix is wrong. (Vocabulary here is fully fictional and disjoint
   from both burned sets and from real product names — a grep for any entity below against
   `backend/services/extraction/` must return zero hits, before and after every change.)
3. **A correction passes a family only if the whole family passes** — positive, paraphrase, passive,
   negative, false-claim trap, and direction trap — not just the item that motivated it.
4. Gold sidecars use `any_of` where multiple canonical mappings are defensible; graders must not
   tighten those post-hoc to match an implementation.

## Families

| File | Failure class (audit stakes) |
|---|---|
| `01_eligibility_open_language` | Closed cue-verb gate killed 30/70 units, 8/29 gold losses |
| `02_argument_alignment` | Exact-surface alignment: 7/29 gold losses, OpenIE lane 0 promoted edges |
| `03_claim_scope_discourse` | Claim-noun complements: both Meridian trap leaks |
| `04_identity_mechanics` | Hyphen slugging (3 FN+FP pairs), version collapse (4 losses), "Dr." splits |
| `05_endpoint_minting_negatives` | Entity P .868 → .631 caused by junk NP minting |
| `06_direction_passive_nominal` | Direction preservation across passive/copular/nominal forms |

## Gold sidecar schema

```json
{
  "fixture": "NN_name.md",
  "items": [
    {
      "id": "X01",
      "check": "positive | paraphrase | passive | nominalized | negative | false_claim_trap | direction_trap | junk_negative | control",
      "evidence": "the exact sentence from the fixture",
      "expect": {
        "type": "canonical | open | qualified | no_edge",
        "subject": "...", "predicate": "...", "object": "...",
        "any_of": ["alternative canonical predicates, when defensible"],
        "surface_predicate": "for open relations",
        "status": "ASSERTED_TRUE | NEGATED | REPORTED_CLAIM | REPORTED_FALSE | HYPOTHETICAL | CONDITIONAL | HISTORICAL_ERROR | UNCERTAIN"
      },
      "must_not": { "promote_positive_edge": true, "entities": ["..."], "notes": "..." }
    }
  ],
  "negative_entities": ["surfaces that must never become graph nodes"],
  "invariants": ["family-level hard checks"]
}
```

Scoring intent: `canonical` items must reach the positive graph with the stated (or any_of) predicate
and direction; `open` items must be *extracted and preserved* as open/unmapped surface relations —
being silently dropped is a failure, being forced into `related_to` is a failure; `qualified` items
must exist as qualified/attributed records and never as positive edges; `no_edge` items must produce
no positive edge (and no listed entity).

# Graphify GLiNER2 + triplet-extract architecture

## Core decomposition

```text
GLiNER2 CPU
= open-world semantic entity discovery and typing

triplet-extract / Stanford-style OpenIE
= open-ended proposition discovery

existing deterministic predicate compiler
= bounded ontology interpretation

existing evidence gate
= authority over durable knowledge
```

## Why this architecture exists

The previous Relex path restored relation recall, but it carries a neural relation pair/scoring cost. The new architecture separates open-class and closed-class work:

- Entity discovery is open-class and benefits from a semantic encoder.
- Surface proposition discovery can be handled by CPU linguistic OpenIE.
- Canonical predicate mapping is bounded and should be deterministic.
- Graph admission must remain evidence-gated.

## Mandatory stages

1. Repository discovery.
2. Baseline capture.
3. Gold-entity relation kill switch.
4. Zero-model corpus survey.
5. GLiNER2 CPU entity census.
6. Raw mention conservation.
7. Document entity reducer.
8. Mention completion.
9. Relation eligibility.
10. triplet-extract OpenIE.
11. OpenIE Argument Adapter.
12. OpenIE Proposition Reducer.
13. Predicate compiler wiring.
14. Claim/assertion assembly.
15. Evidence gate and isolated graph projection.
16. E2E, rebuild, idempotency, quality, speed, reachability verification.

## OpenIE Argument Adapter

Not every OpenIE argument is a canonical entity.

Argument kinds:

```text
ENTITY
LITERAL
DESCRIPTION
EMBEDDED_CLAUSE
UNRESOLVED
```

ENTITY arguments must exact-align to promoted GLiNER2 mentions.

Literals, dates, durations, metrics, and values must remain first-class assertion values rather than fake graph entities.

## OpenIE Proposition Reducer

triplet-extract can emit multiple rendered or entailed variants for a sentence. The reducer must:

- normalize endpoint identity,
- normalize relation lemma,
- preserve attribution/polarity/modality,
- cluster equivalent entailment variants,
- remove exact/subsumed duplicates,
- retain supporting renderings.

Invariant:

```text
many raw OpenIE renderings
→ one canonical proposition family
→ multiple evidence/rendering records
```

## Graph authority

Canonical graph edges require:

- promoted endpoint(s),
- valid proposition,
- exact evidence,
- valid predicate mapping,
- valid direction,
- valid endpoint signature,
- valid textual scope,
- positive asserted status.

Everything else becomes QUALIFIED_CLAIM, OPEN_RELATION, REVIEW, or REJECT.

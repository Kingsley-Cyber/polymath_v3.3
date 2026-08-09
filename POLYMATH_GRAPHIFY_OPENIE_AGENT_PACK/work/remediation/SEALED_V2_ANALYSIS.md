# Sealed-v2 (metadata/spec document) — post-burn layer analysis, 2026-08-07

One-shot result (permanent): 0/26 matched, P/R/F1 0.0, promoted 11 outside band [22,33],
qualified leakage 0/1, projection idempotent, freeze intact, extraction digest-identical
before/after key existence (07ac1e9b…). Owner scorer concurs: relations 0/38, entities 0/60.

## Layer waterfall (computed after the set became development data)

| Layer | Passed | Rate |
|---|---|---|
| Entities discovered (census+reducer, name-matched vs 59 required) | 13/59 | 22% |
| Entities usable as endpoints (promoted+doc_local) | 11/59 | 19% |
| Entities promoted to graph | 7/59 | 12% |
| Directed pairs (promoted, any predicate) | 1/38 | 2.6% |
| Pairs incl. 24 qualified assertions | 1/38 | 2.6% |
| Predicates correct on found pairs | 0 | — |

Pipeline output: 11 edges, dominated by "pegasus 1 5 -detects-> <capability>" + three
XML/YAML supports edges — a prose-relation reading of a metadata document.

## First causal failure: ENTITY DISCOVERY — and it is a hardcoded-schema ceiling

`backend/services/extraction/gliner2_cpu_provider.py:24+` defines ONE module-level
entity-description dictionary ("graphify-entity-descriptions-v1"): Person/Organization/
Location/Event/Concept/Method/Product/Software/Document/Standard/Rule/… — a fixed,
process-wide census schema. The gold's entity classes (DOCUMENT_ID identifiers, schema
fields, format names, span-typed labels) are not describable under it, so census recall
caps at 22% regardless of downstream quality. The predicate space is likewise closed at
detection time through the fixed `Predicate` Literal + `_CANONICAL_BY_LEMMA`-adjacent
tables: the gold's 21 metadata predicates (HAS_PRIMARY_SYSTEM, HAS_TYPE, COMPILED_INTO,
SERVES_AS, NOT_TREATED_AS, …) are unreachable.

This is the entity-layer twin of the audit's original relation finding: the ontology is
still entangled with DETECTION. The relation lane was fixed ("the ontology decides how a
discovered relation is interpreted, never which language the extractor may see"); the
ENTITY lane still violates it.

## Verdict

Credible generalization measurement, jointly of: (a) real underperformance on
metadata-style prose (22% entity discovery), and (b) a task-family ceiling that no
amount of downstream tuning can lift while the census schema is fixed. Not a
regression of the remediation — sealed-v1-as-dev still stands at 40/51, F1 .816,
zero leaks under the same code.

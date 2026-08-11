# ontology

AUTHORITY: config/ontology.yaml:1-16 (entity_types:) AND :18-200 (predicates:) — two enums, one page

COUNT: **15 entity types + 20 predicates**

## Values (verbatim)

### entity_types (ontology.yaml:1-16)

```
Person, Organization, Location, Event, Concept, Method, Product, Software, Document, Standard, Rule, Law, Artifact, TimeReference, other
```

### predicates (ontology.yaml:18-200)

```
includes, uses, supports, produces, implements, has_part, instance_of, references, depends_on, quantizes, causes, synonym_of, member_of, example_of, evaluates, deploys, creates, trains, runs, located_in
```

## Consumers

- graph promotion contract (EXECUTION_PLAN_2026-07-13.md:1061)
- allowed_pairs rules per predicate (ontology.yaml allowed_pairs)

## MUST MATCH

- entity_types (15) = ghost_b_schemas EntityType (15) — value-for-value.
- predicates (20): 11 are shared with the grammar Literal; 9 (has_part, includes, example_of, evaluates, deploys, creates, trains, runs, quantizes) exist ONLY here + spaCy VALID_PREDICATES (spacy_relation_adapter.py:32-35). 5 of those 9 (quantizes, evaluates, deploys, trains, runs) have NO RELATION_ALIAS_MAP entry (ghost_b.py:462-596) — graph-promotion-only predicates; the LLM can never emit them.
- allowed_pairs gate per predicate is consumed by spacy_relation_adapter._load_allowed_pairs (spacy_relation_adapter.py:44) + dep_path_extractor.pair_allowed.

## Bug this guards

- vocab-drift


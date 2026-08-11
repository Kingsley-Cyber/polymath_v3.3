# grammar-schema

AUTHORITY: backend/services/ghost_b_schemas.py:32 (EntityType) AND :53 (Predicate) — two enums, one page

COUNT: **15 entity types + 31 predicates**

## Values (verbatim)

### EntityType (ghost_b_schemas.py:32)

```
Person, Organization, Location, Event, Concept, Method, Product, Software, Document, Standard, Rule, Law, Artifact, TimeReference, other
```

### Predicate (ghost_b_schemas.py:53)

```
part_of, member_of, located_in, works_for, created_by, owns, affiliated_with, synonym_of, instance_of, uses, runs_on, trained_on, references, implements, depends_on, produces, consumes, stores, detects, supports, defines, represents, maps_to, preceded_by, causes, overlaps, derived_from, contradicts, excepts, overrides, related_to
```

## Consumers

- build_schema_native_prompt renders Literal args (ghost_b.py:1803)
- json_schema response_format (ghost_b.py:4705-4710)
- LLMRelation.predicate validation (ghost_b_schemas.py:148)

## MUST MATCH

- EntityType (15) = config/ontology.yaml entity_types (15) — value-for-value (verified by docs/wiki/verify_claims.py).
- Predicate (31) is the ghost_b LLM lane authority; the 9 ontology extensions (has_part, includes, example_of, evaluates, deploys, creates, trains, runs, quantizes) are legal ONLY in the spaCy lane (VALID_PREDICATES, spacy_relation_adapter.py:24-35) and are NOT in this Literal.
- New predicate for the LLM lane → add to this Literal AND UNIVERSAL_RELATION_SCHEMA (ghost_b.py:183) or it dies at validation.
- 5 ontology extensions have NO alias bridge (quantizes/evaluates/deploys/trains/runs — absent from RELATION_ALIAS_MAP ghost_b.py:462-596): an LLM emitting them is rejected, not mapped.

## Bug this guards

- vocab-drift


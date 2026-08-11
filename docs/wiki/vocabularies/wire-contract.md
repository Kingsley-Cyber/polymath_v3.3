# wire-contract

AUTHORITY: backend/models/local_extraction.py:16-63 (EntityType = Literal[...])

COUNT: **34 (25 v1 uppercase + 11 v2 lowercase, minus duplicates in the literal)**

## Values (verbatim)

```
PERSON, ORGANIZATION, PLACE, PRODUCT, DOCUMENT, AGENT, GROUP, SYSTEM, PROCESS, BEHAVIOR, STATE, QUALITY, CONCEPT, METHOD, SIGNAL, BASELINE, METRIC, RESOURCE, CONSTRAINT, GOAL, INTERVENTION, OUTCOME, CONDITION, POPULATION, TIME_PATTERN, person, organization, location, product, software, document, method, concept, event, standard, artifact
```

## Consumers

- 2.8M stored local_extraction.entities[].entity_type (local_extraction.py:44-46)
- ENTITY_TYPE_ALIASES normalizes to ontology (canonical.py:519, entity_quality.py:117)

## MUST MATCH

- Every stored value must be in ENTITY_TYPE_ALIASES keys or an ontology value — removal is a data migration (local_extraction.py:50-51)

## Bug this guards

- v1/v2 dual vocabulary — one corpus never carries two type systems (local_extraction.py:47-48)


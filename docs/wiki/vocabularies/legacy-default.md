# legacy-default

AUTHORITY: backend/services/ghost_b.py:147 (_DEFAULT_ENTITY_TYPES)

COUNT: **4**

## Values (verbatim)

```
person, org, concept, other
```

## Consumers

- open (non-schema) extraction lanes only — ghost_b.py:4105
- build_json_object_prompt teaching a contract the grammar forbids (ghost_b.py:1787-1791)

## MUST MATCH

- NEVER in json_schema lanes. The 4-bucket lowercase enum vs 15 Capitalized grammar collapsed recall (company→Concept, place→Product under token mask).

## Bug this guards

- prompt-vs-grammar


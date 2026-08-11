# fact-types

AUTHORITY: backend/services/ghost_b.py:147-156 (FACT_TYPES) — matches ghost_b_schemas.py:158 (FactType)

COUNT: **9**

## Values (verbatim)

```
property, status, timestamp, quantity, threshold, category, tag, rule_condition, rule_action
```

## Consumers

- LLMFact.fact_type validation (ghost_b_schemas.py:158)
- facts envelope in json_schema mode (ghost_b_schemas.py:164-168)

## MUST MATCH

- Same 9 in ghost_b.py and ghost_b_schemas.py — they are two copies of one enum


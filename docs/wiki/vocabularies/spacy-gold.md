# spacy-gold

AUTHORITY: backend/evals/spacy_relation_asserted_gold_v1.json + RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md:1717

COUNT: **12**

## Values (verbatim)

```
causes, created_by, defines, depends_on, part_of, preceded_by, references, related_to, represents, runs_on, stores, uses
```

## Consumers

- P1 gate fixture (11 samples / 15 asserted relations / 12 predicate types, sha256 8d2912e2…)
- spacy dep-path extraction lane

## MUST MATCH

- Subset of grammar-schema 31 (12/12 present). Gold additions must stay within the 31 or the gate compares against an off-wire vocabulary.

## Bug this guards

- vocab-drift


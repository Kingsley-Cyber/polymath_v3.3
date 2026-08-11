# universal-inline

AUTHORITY: backend/services/ghost_b.py:183-224 (UNIVERSAL_RELATION_SCHEMA)

COUNT: **30 incl. sentinel 'related_to' (ghost_b.py:225)**

## Values (verbatim)

```
part_of, member_of, located_in, works_for, created_by, owns, affiliated_with, synonym_of, instance_of, uses, runs_on, trained_on, references, implements, depends_on, produces, stores, detects, supports, defines, represents, maps_to, preceded_by, causes, overlaps, derived_from, contradicts, excepts, overrides, related_to
```

## Consumers

- resolve_chunk_vocab inline rendering (ghost_b.py:225-229)
- prompt vocabulary constraint block

## MUST MATCH

- UNIVERSAL (30) ⊂ grammar Literal (31); the ONLY difference is `consumes` — present in the Literal (ghost_b_schemas.py:53), gloss map (ghost_b.py:281), and RELATION_ALIAS_MAP (ghost_b.py:476) but ABSENT from the 30 inline list (ghost_b.py:183-224).
- Consequence (verified mechanically): schema lane prompts render the 31 Literal (ghost_b.py:1803) so `consumes` is valid there; json_object lanes render this 30-list (resolve_chunk_vocab, ghost_b.py:1158) so `consumes` is NOT taught — LLMs in that lane either emit it anyway (rejected) or drift to `uses`.
- Sentinel `related_to` MUST stay last (ghost_b.py:222-223); [FALLBACK] tag is rendered from it.

## Bug this guards

- 31 vs 30: 'consumes' is in the grammar Literal but NOT in the 30-list — a canonicalization gap; LLM emitting consumes in non-schema lanes gets alias-mapped or dropped

